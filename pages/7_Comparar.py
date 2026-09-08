from __future__ import annotations

from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3

import streamlit as st

from config import ALLOWED_DOCUMENT_HOSTS, DATABASE_PATH, REVIEW_LOG_PATH
from services.comparison import (
    MAX_COMPARISON_ITEMS,
    MAX_SELECTED_SOURCES,
    MAX_TIMELINE_ROWS,
    MAX_TIMELINE_QUERY_CHARS,
    comparison_source_payload,
    comparison_differences,
    comparison_matrix,
    comparison_to_csv,
    load_selected_decisions,
    outcome_label,
    paginate_timeline,
    printable_html_report,
    request_type_label,
    search_comparison_decisions,
    search_timeline,
    timeline_to_csv,
)
from services.database import database_stats, get_filter_options
from services.effective_records import display_value
from services.pdf_viewer import viewer_session_values
from services.reviews import parse_review_events
from services.ui_helpers import apply_app_style, pdf_page_url


st.set_page_config(page_title="Comparar decisiones", page_icon="⚖️", layout="wide")
apply_app_style()
st.title("Comparar decisiones")
st.caption(
    "Reúne precedentes, identifica qué cambia entre ellos y verifica cada "
    "hallazgo en el acta original."
)

flow_step_1, flow_step_2, flow_step_3 = st.columns(3)
with flow_step_1.container(border=True):
    st.markdown("**1 · Reúne**")
    st.caption("Busca y añade decisiones a tu bandeja.")
with flow_step_2.container(border=True):
    st.markdown("**2 · Contrasta**")
    st.caption("Revisa diferencias, matriz y fuentes.")
with flow_step_3.container(border=True):
    st.markdown("**3 · Sigue el precedente**")
    st.caption("Ordénalo en el tiempo y abre la evidencia.")

MAX_REVIEW_LOG_BYTES = 5_000_000


def open_internal_viewer(item, *, page: int, text: str | None = None) -> None:
    """Abre una evidencia de comparación en el visor compartido."""

    payload = comparison_source_payload(item, page=page, text=text)
    st.session_state.update(
        viewer_session_values(
            payload,
            page=page,
            origin_page="pages/7_Comparar.py",
        )
    )
    st.switch_page("pages/1_Explorador.py")


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
    years: tuple[int, ...],
    origins: tuple[str, ...],
    review_statuses: tuple[str, ...],
    start_date: str | None,
    end_date: str | None,
    review_payload: str,
    database_identity: tuple[int, int],
):
    del database_identity
    return search_timeline(
        DATABASE_PATH,
        field,
        value,
        limit=MAX_TIMELINE_ROWS,
        review_events=parse_review_events(review_payload),
        years=years,
        origins=origins,
        review_statuses=review_statuses,
        start_date=start_date,
        end_date=end_date,
    )


@st.cache_data(show_spinner=False, ttl=1800)
def cached_direct_search(
    query: str,
    page: int,
    page_size: int,
    years: tuple[int, ...],
    database_identity: tuple[int, int],
):
    del database_identity
    return search_comparison_decisions(
        DATABASE_PATH,
        query,
        page=page,
        page_size=page_size,
        years=years,
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


def _widget_token(value: object) -> str:
    """Crea una clave corta y estable para controles repetidos."""

    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


def _decision_identity(item) -> str:
    """Devuelve el dato más útil para reconocer una decisión de un vistazo."""

    return str(
        item.product_name
        or item.active_ingredient
        or item.expediente
        or item.radicado
        or item.title
    )


def _outcome_marker(value: str | None) -> str:
    normalized = str(value or "").casefold()
    negative_favorable = any(
        phrase in normalized
        for phrase in (
            "no_favorable",
            "no favorable",
            "no es favorable",
            "no resulta favorable",
        )
    )
    if (
        negative_favorable
        or "desfavorable" in normalized
        or "negad" in normalized
        or "rechaz" in normalized
    ):
        return "🔴"
    if "favorable" in normalized or "aprobad" in normalized:
        return "🟢"
    if "requer" in normalized or "pendiente" in normalized:
        return "🟡"
    return "🔵"


def _match_origin_display(value: str | None) -> str:
    """Agrupa procedencias heredadas bajo etiquetas orientadas a consulta."""

    if value in {"structured", "verified", "reviewed", None, ""}:
        return "Ficha estructurada"
    if value == "inferred":
        return "Inferida automáticamente"
    if value == "textual":
        return "Mención textual"
    return "Coincidencia documental"


def render_timeline_card(item, *, number: int) -> None:
    """Presenta un hito cronológico compacto con acceso inmediato a la fuente."""

    target_page = item.match_page or item.page_number
    source_url = pdf_page_url(item.url, target_page, ALLOWED_DOCUMENT_HOSTS)
    token = _widget_token(f"{number}:{item.review_identifier}:{target_page}")
    with st.container(border=True):
        marker_col, content_col, action_col = st.columns([0.75, 4.25, 1.35])
        marker_col.markdown(
            f"### {_outcome_marker(item.outcome_code)} "
            f"{display_value(item.year)}"
        )
        marker_col.caption(display_value(item.session_date))

        content_col.markdown(f"**{_decision_identity(item)}**")
        content_col.caption(
            f"Acta {display_value(item.acta_number)} · "
            f"{request_type_label(item.request_type)} · "
            f"{outcome_label(item.outcome_code)}"
        )
        identity_parts = []
        if item.numeral:
            identity_parts.append(f"Numeral {item.numeral}")
        if item.expediente:
            identity_parts.append(f"Expediente {item.expediente}")
        if item.radicado:
            identity_parts.append(f"Radicado {item.radicado}")
        identity_parts.append(
            f"{_match_origin_display(item.match_origin)} · pág. {target_page}"
        )
        content_col.caption(" · ".join(identity_parts))

        if action_col.button(
            "Ver en la app",
            key=f"timeline_card_view_{token}",
            use_container_width=True,
        ):
            open_internal_viewer(
                item,
                page=target_page,
                text=(
                    item.match_evidence
                    or f"Solicitud\n{item.request_text or ''}\n\n"
                    f"Concepto\n{item.concept_text or ''}"
                ),
            )
        if source_url:
            action_col.link_button(
                "Abrir PDF",
                source_url,
                key=f"timeline_card_pdf_{token}",
                use_container_width=True,
            )


if database_stats(DATABASE_PATH)["documents"] == 0:
    st.warning("No existe un índice. Ejecuta Construir índice desde GitHub Actions.")
    st.stop()

try:
    if (
        REVIEW_LOG_PATH.exists()
        and REVIEW_LOG_PATH.stat().st_size > MAX_REVIEW_LOG_BYTES
    ):
        raise ValueError("el archivo auxiliar supera 5 MB")
    review_payload = (
        REVIEW_LOG_PATH.read_text(encoding="utf-8-sig")
        if REVIEW_LOG_PATH.exists()
        else ""
    )
    review_events = parse_review_events(review_payload)
except (OSError, UnicodeDecodeError, ValueError):
    st.error(
        "No se puede abrir la comparación de forma coherente porque un archivo "
        "auxiliar no está disponible o no es compatible."
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

filter_options = get_filter_options(DATABASE_PATH, review_events=review_events)
st.subheader("1. Reúne las decisiones")
st.caption(
    "Puedes traer evidencias desde el Explorador o buscar fichas directamente "
    "sin salir de esta página."
)
with st.expander("🔎 Buscar decisiones para comparar", expanded=not raw_selected):
    st.caption(
        "Busca por producto, principio activo, interesado, expediente, radicado, "
        "solicitud o concepto. No necesitas regresar al Explorador."
    )
    previous_direct_request = st.session_state.get("comparison_direct_request", {})
    if not isinstance(previous_direct_request, dict):
        previous_direct_request = {}
    direct_year_options = list(filter_options.get("years", []))
    direct_default_years = [
        year
        for year in previous_direct_request.get("years", ())
        if year in direct_year_options
    ]
    with st.form("comparison_direct_search_form"):
        direct_col1, direct_col2, direct_col3 = st.columns([2.4, 1.25, 0.85])
        direct_query = direct_col1.text_input(
            "Producto, principio activo o identificador",
            value=str(previous_direct_request.get("query", "")),
            placeholder="Ej.: semaglutida, Novo Nordisk o expediente",
            max_chars=MAX_TIMELINE_QUERY_CHARS,
        )
        direct_years = direct_col2.multiselect(
            "Año (opcional)",
            options=direct_year_options,
            default=direct_default_years,
        )
        direct_page_size = direct_col3.selectbox(
            "Por página",
            options=[5, 10, 20, 25],
            index=[5, 10, 20, 25].index(
                int(previous_direct_request.get("page_size", 10))
            )
            if int(previous_direct_request.get("page_size", 10)) in [5, 10, 20, 25]
            else 1,
        )
        direct_submit = st.form_submit_button(
            "Buscar decisiones",
            type="primary",
        )
    if direct_submit:
        st.session_state["comparison_direct_request"] = {
            "query": direct_query.strip(),
            "years": tuple(int(year) for year in direct_years),
            "page_size": int(direct_page_size),
            "page": 1,
        }

    direct_request = st.session_state.get("comparison_direct_request")
    if direct_request:
        try:
            direct_page = cached_direct_search(
                str(direct_request["query"]),
                int(direct_request.get("page", 1)),
                int(direct_request.get("page_size", 10)),
                tuple(int(year) for year in direct_request.get("years", ())),
                _file_identity(DATABASE_PATH),
            )
        except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
            st.error(f"No fue posible buscar decisiones: {exc}")
        else:
            result_heading, result_count = st.columns([3, 1])
            result_heading.markdown("**Resultados disponibles**")
            result_count.caption(
                f"{direct_page.total_matches} coincidencias · "
                f"página {direct_page.page}/{direct_page.total_pages}"
            )
            selected_uids = {
                str(item.get("decision_uid") or "")
                for item in raw_selected
                if isinstance(item, dict)
            }
            pending_items = []
            if not direct_page.items:
                st.info(
                    "No encontramos decisiones con esos términos. Prueba con "
                    "el principio activo, el interesado o un número sin puntuación."
                )
            for item in direct_page.items:
                item_digest = hashlib.sha256(
                    item.decision_uid.encode("utf-8")
                ).hexdigest()[:12]
                with st.container(border=True):
                    item_columns = st.columns([4.4, 1])
                    already_selected = item.decision_uid in selected_uids
                    item_columns[0].markdown(f"**{item.label}**")
                    item_columns[0].caption(
                        f"{display_value(item.active_ingredient)} · "
                        f"{request_type_label(item.request_type)} · "
                        f"{outcome_label(item.outcome_code)}"
                    )
                    picked = item_columns[1].checkbox(
                        "En bandeja" if already_selected else "Añadir",
                        value=already_selected,
                        disabled=already_selected,
                        key=f"comparison_direct_pick_{item_digest}",
                    )
                if picked and not already_selected:
                    pending_items.append(item)
            if st.button(
                "Añadir marcadas a la bandeja",
                disabled=not pending_items,
                key="comparison_direct_add",
                type="primary",
            ):
                selected_store: dict[str, dict] = st.session_state.setdefault(
                    "selected_evidence",
                    {},
                )
                available = max(0, MAX_SELECTED_SOURCES - len(selected_store))
                for item in pending_items[:available]:
                    selection_id = (
                        f"direct:{item.decision_uid}:"
                        f"{item.selection['chunk_id']}"
                    )
                    selected_store[selection_id] = dict(item.selection)
                st.session_state.pop("comparison_selected_sources", None)
                st.rerun()

            direct_prev, direct_middle, direct_next = st.columns([1, 2, 1])
            if direct_prev.button(
                "← Anterior",
                disabled=direct_page.page <= 1,
                key="comparison_direct_previous",
                use_container_width=True,
            ):
                direct_request["page"] = direct_page.page - 1
                st.session_state["comparison_direct_request"] = direct_request
                st.rerun()
            direct_middle.caption(
                f"Resultados {(direct_page.page - 1) * direct_page.page_size + 1}–"
                f"{min(direct_page.page * direct_page.page_size, direct_page.total_matches)}"
                if direct_page.total_matches
                else "Sin coincidencias"
            )
            if direct_next.button(
                "Siguiente →",
                disabled=direct_page.page >= direct_page.total_pages,
                key="comparison_direct_next",
                use_container_width=True,
            ):
                direct_request["page"] = direct_page.page + 1
                st.session_state["comparison_direct_request"] = direct_request
                st.rerun()

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
            if str(key).startswith(("select_evidence_", "select_reviewed_")):
                del st.session_state[key]
        st.session_state.pop("comparison_selected_sources", None)
        st.rerun()

st.subheader("2. Contrasta las decisiones")
if not decisions:
    with st.container(border=True):
        st.markdown("**Tu bandeja está vacía**")
        if raw_selected:
            st.warning(
                "La selección ya no coincide con el índice vigente. Límpiala y "
                "vuelve a seleccionar las evidencias."
            )
        else:
            st.write(
                "Busca decisiones en el paso 1 o selecciónalas desde el Explorador. "
                "Puedes construir una cronología aunque no prepares una comparación."
            )
            st.page_link(
                "pages/1_Explorador.py",
                label="Ir al Explorador",
                icon="🔍",
            )
    selected_decisions = []
else:
    decision_by_key = {decision.key: decision for decision in decisions}
    default_keys = list(decision_by_key)[: min(4, len(decision_by_key))]
    with st.container(border=True):
        tray_heading, tray_count = st.columns([4, 1])
        tray_heading.markdown("**Bandeja de decisiones**")
        tray_heading.caption(
            "Elige cuáles quieres mostrar lado a lado. Cada ficha aparece una sola "
            "vez aunque tenga varias evidencias."
        )
        tray_count.metric("Disponibles", len(decisions))
        chosen_keys = st.multiselect(
            "Decisiones para contrastar",
            options=list(decision_by_key),
            default=default_keys,
            format_func=lambda value: decision_by_key[value].label,
            max_selections=min(8, MAX_COMPARISON_ITEMS),
            help="Puedes contrastar hasta ocho decisiones simultáneamente.",
            label_visibility="collapsed",
        )
    selected_decisions = [decision_by_key[key] for key in chosen_keys]

    if selected_decisions:
        differences = comparison_differences(selected_decisions)
        card_columns = st.columns(min(4, len(selected_decisions)))
        for index, decision in enumerate(selected_decisions, start=1):
            with card_columns[(index - 1) % len(card_columns)].container(border=True):
                st.markdown(f"**D{index} · {_decision_identity(decision)}**")
                st.caption(
                    f"Acta {display_value(decision.acta_number)} "
                    f"({display_value(decision.year)}) · "
                    f"{outcome_label(decision.outcome_code)}"
                )

        differences_tab, matrix_tab, sources_tab = st.tabs(
            [
                f"Δ Diferencias ({len(differences)})",
                "Matriz completa",
                "Fuentes",
            ]
        )
        with differences_tab:
            if len(selected_decisions) < 2:
                st.info(
                    "Añade una segunda decisión para identificar diferencias. "
                    "La ficha seleccionada ya puede consultarse en las otras vistas."
                )
            elif not differences:
                st.success("No se detectaron diferencias en los campos comparables.")
            else:
                st.caption(
                    "Solo aparecen los campos que cambian. D1, D2, etc. identifican "
                    "las tarjetas superiores."
                )
                for difference in differences:
                    with st.container(border=True):
                        field_col, count_col = st.columns([4, 1])
                        field_col.markdown(f"**{difference['Campo']}**")
                        count_col.caption(
                            f"{difference['Valores distintos']} valores"
                        )
                        for detail_line in str(difference["Detalle"]).splitlines():
                            st.caption(detail_line)

        with matrix_tab:
            st.caption(
                "Desplázate horizontalmente para revisar las decisiones completas. "
                "Los textos extensos también quedan disponibles en la descarga CSV."
            )
            st.dataframe(
                [
                    row
                    for row in comparison_matrix(selected_decisions)
                    if row.get("Campo")
                    in {
                        "Acta",
                        "Año",
                        "Numeral",
                        "Título del numeral",
                        "Fecha de sesión",
                        "Producto",
                        "Principio activo",
                        "Interesado",
                        "Expediente",
                        "Radicado",
                        "Tipo de solicitud",
                        "Resultado",
                        "Páginas",
                        "Solicitud",
                        "Concepto",
                        "Evidencia seleccionada",
                    }
                ],
                hide_index=True,
                use_container_width=True,
                height=565,
            )

        with sources_tab:
            st.caption(
                "Abre cada evidencia dentro de la aplicación o directamente en el "
                "PDF oficial."
            )
            for index, decision in enumerate(selected_decisions, start=1):
                with st.expander(f"D{index} · {decision.label}"):
                    metadata = [decision.title]
                    if decision.section:
                        metadata.append(decision.section)
                    if decision.part:
                        metadata.append(decision.part)
                    metadata.append(outcome_label(decision.outcome_code))
                    if decision.record_id is None:
                        metadata.append("fragmento sin ficha estructurada")
                    elif decision.needs_review:
                        metadata.append("extracción automática")
                    st.caption(" · ".join(metadata))
                    for evidence_index, evidence in enumerate(
                        decision.evidences,
                        start=1,
                    ):
                        st.markdown(f"**Evidencia {evidence_index} · página {evidence.page}**")
                        st.write(evidence.text)
                        evidence_url = pdf_page_url(
                            decision.url,
                            evidence.page,
                            ALLOWED_DOCUMENT_HOSTS,
                        )
                        internal_col, external_col = st.columns(2)
                        if internal_col.button(
                            "Ver en la app",
                            key=(
                                f"comparison_view_{index}_{evidence_index}_"
                                f"{evidence.chunk_id}"
                            ),
                            use_container_width=True,
                        ):
                            open_internal_viewer(
                                decision,
                                page=evidence.page,
                                text=evidence.text,
                            )
                        if evidence_url:
                            external_col.link_button(
                                f"Abrir PDF · pág. {evidence.page}",
                                evidence_url,
                                key=(
                                    f"comparison_pdf_{index}_{evidence_index}_"
                                    f"{_widget_token(evidence.chunk_id)}"
                                ),
                                use_container_width=True,
                            )
                    source_url = pdf_page_url(
                        decision.url,
                        decision.page_number,
                        ALLOWED_DOCUMENT_HOSTS,
                    )
                    source_internal, source_external = st.columns(2)
                    if source_internal.button(
                        "Ver ficha en la app",
                        key=f"comparison_view_record_{index}_{decision.key}",
                        use_container_width=True,
                    ):
                        open_internal_viewer(
                            decision,
                            page=decision.page_number,
                            text=(
                                f"Solicitud\n{decision.request_text or ''}\n\n"
                                f"Concepto\n{decision.concept_text or ''}"
                            ),
                        )
                    if source_url:
                        source_external.link_button(
                            f"Abrir PDF · pág. {decision.page_number}",
                            source_url,
                            key=(
                                f"comparison_record_pdf_{index}_"
                                f"{_widget_token(decision.key)}"
                            ),
                            use_container_width=True,
                        )

        with st.container(border=True):
            export_left, export_right = st.columns([1, 2])
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
                "El reporte imprimible aparece al final y puede combinar esta "
                "comparación con una cronología."
            )
    else:
        st.info("Selecciona al menos una decisión de la bandeja para comenzar.")

st.divider()
st.subheader("3. Sigue el precedente en el tiempo")
st.write(
    "Busca fichas estructuradas y menciones en el texto completo. La línea "
    "temporal distingue el origen para que puedas verificar cada coincidencia."
)

timeline_field_labels = {
    "Producto": "product",
    "Principio activo": "active_ingredient",
    "Interesado": "interested_party",
    "Expediente": "expediente",
    "Radicado": "radicado",
    "Resultado": "outcome",
    "Tipo de solicitud": "request_type",
}
timeline_field_col, timeline_value_col, timeline_size_col = st.columns([1.2, 2.5, 0.8])
timeline_field_label = timeline_field_col.selectbox(
    "Organizar por",
    timeline_field_labels,
)
timeline_field = timeline_field_labels[timeline_field_label]

candidate_attribute = {
    "product": "product_name",
    "active_ingredient": "active_ingredient",
    "interested_party": "interested_party",
    "expediente": "expediente",
    "radicado": "radicado",
    "outcome": "outcome_code",
    "request_type": "request_type",
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
timeline_value = timeline_value_col.text_input(
    f"{timeline_field_label} a rastrear",
    value=default_value,
    key=f"timeline_input_{timeline_field}",
    placeholder="Escribe un valor extraído de las actas",
    max_chars=MAX_TIMELINE_QUERY_CHARS,
)
timeline_page_size = timeline_size_col.selectbox(
    "Por página",
    options=[10, 25, 50, 100],
    index=1,
)

with st.expander("⚙️ Acotar la cronología", expanded=False):
    filter_col1, filter_col2, filter_col3 = st.columns([1.15, 1.45, 1.4])
    timeline_years = filter_col1.multiselect(
        "Año",
        options=filter_options.get("years", []),
        help="Vacío incluye todos los años.",
    )
    origin_options = {
        "Ficha estructurada": ("structured", "verified", "reviewed"),
        "Inferida automáticamente": ("inferred",),
        "Mención textual": ("textual",),
    }
    selected_origin_labels = filter_col2.multiselect(
        "Origen de la coincidencia",
        options=list(origin_options),
        help="Vacío incluye todas las procedencias.",
    )
    timeline_date_range = filter_col3.date_input(
        "Rango de fechas de sesión (opcional)",
        value=(),
        min_value=date(1900, 1, 1),
        max_value=date(2200, 12, 31),
        format="YYYY-MM-DD",
        help=(
            "Selecciona fecha inicial y final. Las fichas sin fecha de sesión "
            "no se incluyen cuando este filtro está activo."
        ),
    )
timeline_origins = tuple(
    origin
    for label in selected_origin_labels
    for origin in origin_options[label]
)
timeline_statuses: tuple[str, ...] = ()
timeline_start_date = (
    timeline_date_range[0].isoformat()
    if isinstance(timeline_date_range, (tuple, list)) and len(timeline_date_range) == 2
    else None
)
timeline_end_date = (
    timeline_date_range[1].isoformat()
    if isinstance(timeline_date_range, (tuple, list)) and len(timeline_date_range) == 2
    else None
)
if isinstance(timeline_date_range, (tuple, list)) and len(timeline_date_range) == 1:
    st.info("Selecciona también la fecha final para aplicar el rango de sesión.")

if st.button("Construir cronología", type="primary"):
    st.session_state.pop("comparison_timeline_request", None)
    try:
        timeline_results = cached_timeline(
            timeline_field,
            timeline_value.strip(),
            tuple(int(year) for year in timeline_years),
            timeline_origins,
            timeline_statuses,
            timeline_start_date,
            timeline_end_date,
            review_payload,
            _file_identity(DATABASE_PATH),
        )
        st.session_state["comparison_timeline_request"] = {
            "field": timeline_field,
            "field_label": timeline_field_label,
            "value": timeline_value.strip(),
            "page": 1,
            "page_size": timeline_page_size,
            "years": tuple(int(year) for year in timeline_years),
            "origins": timeline_origins,
            "review_statuses": timeline_statuses,
            "start_date": timeline_start_date,
            "end_date": timeline_end_date,
        }
    except (ValueError, sqlite3.Error) as exc:
        st.error(str(exc))

timeline_request = st.session_state.get("comparison_timeline_request")
timeline_page = None
timeline_results = []
if timeline_request:
    try:
        timeline_all_results = cached_timeline(
            timeline_request["field"],
            timeline_request["value"],
            tuple(int(year) for year in timeline_request.get("years", ())),
            tuple(timeline_request.get("origins", ())),
            tuple(timeline_request.get("review_statuses", ())),
            timeline_request.get("start_date"),
            timeline_request.get("end_date"),
            review_payload,
            _file_identity(DATABASE_PATH),
        )
        timeline_page = paginate_timeline(
            timeline_all_results,
            page=int(timeline_request.get("page", 1)),
            page_size=int(timeline_request.get("page_size", 25)),
        )
        timeline_results = list(timeline_page.entries)
    except (KeyError, TypeError, ValueError, sqlite3.Error) as exc:
        st.session_state.pop("comparison_timeline_request", None)
        st.error(f"No fue posible construir la cronología: {exc}")

if timeline_request and not timeline_results:
    st.warning(
        "No se encontraron fichas estructuradas ni menciones textuales para ese "
        "valor con los filtros seleccionados."
    )
    render_comparison_report_download(selected_decisions)
elif timeline_results:
    total_label = (
        f"al menos {timeline_page.total_loaded}"
        if timeline_page and timeline_page.truncated
        else str(timeline_page.total_loaded if timeline_page else len(timeline_results))
    )
    st.success(
        f"{total_label} decisiones para {timeline_request['field_label']}: "
        f"{timeline_request['value']}"
    )
    if timeline_page and timeline_page.truncated:
        st.warning(
            "La cronología alcanzó el límite de seguridad de 250 decisiones. "
            "Acota el valor, los años o las fechas para obtener un total exacto."
        )
    if timeline_page and timeline_page.total_pages > 1:
        timeline_prev, timeline_middle, timeline_next = st.columns([1, 2, 1])
        if timeline_prev.button(
            "← Página anterior",
            disabled=timeline_page.page <= 1,
            key="timeline_previous_page",
            use_container_width=True,
        ):
            timeline_request["page"] = timeline_page.page - 1
            st.session_state["comparison_timeline_request"] = timeline_request
            st.rerun()
        timeline_middle.caption(
            f"Página {timeline_page.page} de {timeline_page.total_pages} · "
            f"{len(timeline_results)} decisiones en esta página"
        )
        if timeline_next.button(
            "Página siguiente →",
            disabled=timeline_page.page >= timeline_page.total_pages,
            key="timeline_next_page",
            use_container_width=True,
        ):
            timeline_request["page"] = timeline_page.page + 1
            st.session_state["comparison_timeline_request"] = timeline_request
            st.rerun()
    timeline_table = [
        {
            "Año": display_value(item.year),
            "Acta": display_value(item.acta_number),
            "Página": item.page_number,
            "Fecha de sesión": display_value(item.session_date),
            "Numeral": display_value(item.numeral),
            "Producto": display_value(item.product_name),
            "Principio activo": display_value(item.active_ingredient),
            "Expediente": display_value(item.expediente),
            "Radicado": display_value(item.radicado),
            "Resultado": outcome_label(item.outcome_code),
            "Tipo de solicitud": request_type_label(item.request_type),
            "Origen de coincidencia": _match_origin_display(item.match_origin),
            "Confianza": (
                round(item.confidence, 2)
                if item.confidence is not None
                else "No aplica"
            ),
            "Página de coincidencia": item.match_page,
        }
        for item in timeline_results
    ]
    first_result_number = (
        (timeline_page.page - 1) * timeline_page.page_size + 1
        if timeline_page
        else 1
    )
    visual_tab, table_tab, detail_tab = st.tabs(
        ["Línea de tiempo", "Tabla", "Evidencias y conceptos"]
    )
    with visual_tab:
        st.caption(
            "Orden cronológico · 🟢 favorable · 🔴 desfavorable · "
            "🟡 requiere atención · 🔵 otro resultado"
        )
        visible_timeline = timeline_results[:25]
        for index, item in enumerate(
            visible_timeline,
            start=first_result_number,
        ):
            render_timeline_card(item, number=index)
        if len(timeline_results) > len(visible_timeline):
            st.info(
                "Esta vista visual muestra los primeros 25 hitos de la página. "
                "La tabla y el CSV conservan todos los resultados."
            )

    with table_tab:
        st.caption(
            "Vista compacta para ordenar, inspeccionar columnas y comparar varios "
            "hitos a la vez."
        )
        st.dataframe(
            timeline_table,
            hide_index=True,
            use_container_width=True,
            height=min(560, 38 + 35 * len(timeline_table)),
        )

    with detail_tab:
        st.caption(
            "Consulta la solicitud, el concepto y el fragmento que produjo cada "
            "coincidencia."
        )
        for index, item in enumerate(
            timeline_results[:50],
            start=first_result_number,
        ):
            with st.expander(
                f"{index}. {_decision_identity(item)} · "
                f"Acta {display_value(item.acta_number)} · "
                f"{outcome_label(item.outcome_code)}"
            ):
                st.markdown(
                    f"**Origen de la coincidencia:** "
                    f"{_match_origin_display(item.match_origin)}"
                )
                if item.match_origin == "textual":
                    st.warning(
                        "Esta es una mención en el texto. No confirma por sí sola "
                        f"que {timeline_request['value']} sea el "
                        f"{timeline_request['field_label'].lower()} de la decisión."
                    )
                elif item.match_origin == "inferred":
                    st.info(
                        "Este valor fue inferido automáticamente a partir del "
                        "contexto. Compruébalo en la evidencia antes de usarlo "
                        "como dato estructurado."
                    )
                if item.match_evidence and not item.match_evidences:
                    st.markdown(
                        f"**Evidencia de coincidencia · página "
                        f"{item.match_page or item.page_number}**"
                    )
                    st.write(item.match_evidence)
                if item.match_evidences:
                    st.markdown("**Evidencias textuales agrupadas**")
                    for evidence_index, evidence in enumerate(
                        item.match_evidences,
                        start=1,
                    ):
                        st.caption(f"Página {evidence.page} · F{evidence.chunk_id}")
                        st.write(evidence.text)
                        mention_url = pdf_page_url(
                            item.url,
                            evidence.page,
                            ALLOWED_DOCUMENT_HOSTS,
                        )
                        mention_internal, mention_external = st.columns(2)
                        if mention_internal.button(
                            "Ver en la app",
                            key=(
                                f"timeline_view_mention_{index}_{evidence_index}_"
                                f"{evidence.chunk_id}"
                            ),
                            use_container_width=True,
                        ):
                            open_internal_viewer(
                                item,
                                page=evidence.page,
                                text=evidence.text,
                            )
                        if mention_url:
                            mention_external.link_button(
                                f"Abrir PDF · pág. {evidence.page}",
                                mention_url,
                                key=(
                                    f"timeline_mention_pdf_{index}_{evidence_index}_"
                                    f"{_widget_token(evidence.chunk_id)}"
                                ),
                                use_container_width=True,
                            )
                st.markdown("**Solicitud**")
                st.write(display_value(item.request_text))
                st.markdown("**Concepto**")
                st.write(display_value(item.concept_text))
                source_url = pdf_page_url(
                    item.url,
                    item.match_page or item.page_number,
                    ALLOWED_DOCUMENT_HOSTS,
                )
                source_internal, source_external = st.columns(2)
                target_page = item.match_page or item.page_number
                if source_internal.button(
                    "Ver en la app",
                    key=(
                        f"timeline_view_source_{index}_"
                        f"{_widget_token(item.review_identifier)}"
                    ),
                    use_container_width=True,
                ):
                    open_internal_viewer(
                        item,
                        page=target_page,
                        text=(
                            item.match_evidence
                            or f"Solicitud\n{item.request_text or ''}\n\n"
                            f"Concepto\n{item.concept_text or ''}"
                        ),
                    )
                if source_url:
                    source_external.link_button(
                        f"Abrir PDF · pág. {target_page}",
                        source_url,
                        key=(
                            f"timeline_source_pdf_{index}_"
                            f"{_widget_token(item.review_identifier)}"
                        ),
                        use_container_width=True,
                    )
                if item.decision_uid:
                    st.caption("Identificador estable de la decisión")
                    st.code(item.decision_uid, language=None)
        if len(timeline_results) > 50:
            st.caption(
                "Se muestran 50 detalles en esta página; la tabla y el CSV "
                "conservan todos sus resultados."
            )

    timeline_download, report_download = st.columns(2)
    try:
        timeline_csv = timeline_to_csv(timeline_results)
    except ValueError as exc:
        timeline_download.error(str(exc))
    else:
        timeline_download.download_button(
            "Descargar esta página de la cronología CSV",
            data=timeline_csv,
            file_name="cronologia_invima.csv",
            mime="text/csv",
            use_container_width=True,
        )
    report_title = (
        "Comparación INVIMA y página de cronología de "
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
    with st.container(border=True):
        st.markdown("**La cronología está lista para configurarse**")
        st.write(
            "Elige un campo y escribe el valor que quieres rastrear. Si proviene "
            "de la bandeja, la aplicación ya lo propone en el formulario."
        )
    render_comparison_report_download(selected_decisions)
else:
    with st.container(border=True):
        st.markdown("**Aún no has construido una cronología**")
        st.write(
            "Selecciona el tipo de dato, escribe un valor y pulsa "
            "**Construir cronología**. No necesitas preparar una comparación antes."
        )

st.caption(
    "Las fichas y resultados se extraen automáticamente. Verifica cualquier "
    "conclusión contra la página enlazada antes de usarla."
)
