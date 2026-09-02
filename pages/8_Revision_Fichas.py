from __future__ import annotations

import hmac
import os

import streamlit as st

from config import DATABASE_PATH, REVIEW_LOG_PATH
from services.database import (
    count_regulatory_records,
    get_filter_options,
    get_regulatory_records_by_uids,
    list_regulatory_records,
)
from services.reviews import (
    OUTCOME_CODES,
    REQUEST_TYPE_CODES,
    REVIEW_FIELDS,
    append_review_event,
    apply_latest_reviews,
    load_review_events,
    new_review_event,
    parse_review_events,
    serialize_review_events,
    write_review_events,
)
from services.ui_helpers import pdf_page_url
from config import ALLOWED_DOCUMENT_HOSTS


st.set_page_config(page_title="Revisión de fichas", page_icon="📝", layout="wide")
st.title("📝 Revisión de fichas regulatorias")
st.caption(
    "Corrige y aprueba la extracción automática sin modificar el texto oficial."
)

try:
    secrets = dict(st.secrets)
except Exception:
    secrets = {}
configured_password = str(
    secrets.get("ADMIN_PASSWORD", os.getenv("ADMIN_PASSWORD", ""))
).strip()
if not configured_password:
    st.warning("Configura ADMIN_PASSWORD para habilitar la revisión de fichas.")
    st.stop()

provided_password = st.text_input("Contraseña de revisión", type="password")
if not provided_password or not hmac.compare_digest(
    provided_password, configured_password
):
    st.info("Ingresa la contraseña de administración para continuar.")
    st.stop()

saved_message = st.session_state.pop("review_saved_message", "")
if saved_message:
    st.success(saved_message)

options = get_filter_options(DATABASE_PATH)
with st.sidebar:
    st.header("Cola de revisión")
    query = st.text_input("Producto, expediente, radicado o numeral")
    years = st.multiselect("Año", options.get("years", []))
    outcomes = st.multiselect("Resultado", options.get("outcomes", []))
    request_types = st.multiselect(
        "Tipo de solicitud", options.get("request_types", [])
    )
    missing_label = st.selectbox(
        "Priorizar campo faltante",
        [
            "Todos",
            "Numeral",
            "Fecha de sesión",
            "Tipo de solicitud",
            "Producto",
            "Resultado",
        ],
    )

missing_mapping = {
    "Todos": "",
    "Numeral": "numeral",
    "Fecha de sesión": "session_date",
    "Tipo de solicitud": "request_type",
    "Producto": "product",
    "Resultado": "outcome",
}
total_records = count_regulatory_records(
    DATABASE_PATH,
    query=query,
    years=years,
    outcomes=outcomes,
    request_types=request_types,
    missing_field=missing_mapping[missing_label],
)
page_size = 100
page_count = max(1, (total_records + page_size - 1) // page_size)
queue_page_key = "review_queue_page"
stored_queue_page = int(st.session_state.get(queue_page_key, 1) or 1)
if stored_queue_page < 1 or stored_queue_page > page_count:
    st.session_state[queue_page_key] = 1
with st.sidebar:
    page_number_filter = st.number_input(
        "Página de resultados",
        min_value=1,
        max_value=page_count,
        step=1,
        key=queue_page_key,
    )
records = list_regulatory_records(
    DATABASE_PATH,
    query=query,
    years=years,
    outcomes=outcomes,
    request_types=request_types,
    missing_field=missing_mapping[missing_label],
    limit=page_size,
    offset=(int(page_number_filter) - 1) * page_size,
)
try:
    events = load_review_events(REVIEW_LOG_PATH)
except ValueError as exc:
    st.error(f"El registro de revisiones no es válido: {exc}")
    events = []
event_uids = sorted({event.decision_uid for event in events})
known_event_uids = {
    str(item.get("decision_uid") or "")
    for item in get_regulatory_records_by_uids(DATABASE_PATH, event_uids)
}
orphan_event_uids = set(event_uids) - known_event_uids
if orphan_event_uids:
    st.warning(
        f"Hay {len(orphan_event_uids)} ficha(s) revisada(s) que ya no existen "
        "en la extracción vigente. El historial se conserva, pero no se aplica."
    )
effective_records = apply_latest_reviews(records, events)

metric1, metric2, metric3, metric4, metric5 = st.columns(5)
metric1.metric("Coincidencias totales", total_records)
metric2.metric("Fichas en esta página", len(effective_records))
metric3.metric(
    "Automáticas",
    sum(record.get("review_status") == "automatic" for record in effective_records),
)
metric4.metric(
    "Revisadas",
    sum(record.get("review_status") == "reviewed" for record in effective_records),
)
metric5.metric(
    "Aprobadas",
    sum(record.get("review_status") == "approved" for record in effective_records),
)

if not effective_records:
    st.info("No hay fichas que coincidan con los filtros.")
    st.stop()


def record_label(record: dict) -> str:
    product = record.get("product_name") or "Producto sin extraer"
    numeral = record.get("numeral") or "sin numeral"
    return (
        f"{record.get('year') or 's/a'} · Acta {record.get('acta_number') or '?'} "
        f"· {numeral} · {product}"
    )


selection_signature = (
    query,
    tuple(years),
    tuple(outcomes),
    tuple(request_types),
    missing_label,
    int(page_number_filter),
)
if st.session_state.get("review_selection_signature") != selection_signature:
    st.session_state["review_selection_signature"] = selection_signature
    st.session_state["review_record_index"] = 0
selected_index = st.selectbox(
    "Ficha a revisar",
    range(len(effective_records)),
    format_func=lambda index: record_label(effective_records[index]),
    key="review_record_index",
)
record = effective_records[int(selected_index)]
automatic = record.get("automatic_values") or {}

source_col, status_col = st.columns([2, 1])
with source_col:
    st.markdown(f"### {record.get('title')}")
    st.caption(
        f"Páginas {record.get('page_number')}–{record.get('end_page_number')} · "
        f"ID {record.get('decision_uid')}"
    )
    st.link_button(
        "Abrir fuente en la página",
        pdf_page_url(
            str(record.get("url") or ""),
            int(record.get("page_number") or 1),
            ALLOWED_DOCUMENT_HOSTS,
        ),
    )
with status_col:
    status_labels = {
        "automatic": "Automática",
        "reviewed": "Revisada",
        "approved": "Aprobada",
        "reopened": "Reabierta",
        "stale": "Revisión desactualizada",
    }
    st.metric("Estado", status_labels.get(str(record.get("review_status")), "Automática"))
    if record.get("reviewer"):
        st.caption(
            f"{record.get('reviewer')} · {record.get('reviewed_at', '')}"
        )
    if record.get("review_stale"):
        st.warning(
            "El PDF o la extracción cambió desde esta revisión. Verifica "
            "nuevamente antes de aprobar."
        )

with st.expander("Valores automáticos de referencia", expanded=False):
    st.caption(
        "Estos valores provienen del extractor. El formulario muestra el valor "
        "vigente, incluida una corrección humana cuando existe."
    )
    field_labels = {
        "numeral": "Numeral",
        "numeral_title": "Título del numeral",
        "session_date": "Fecha de sesión",
        "request_type_code": "Tipo de solicitud",
        "product_name": "Producto",
        "active_ingredient": "Principio activo",
        "interested_party": "Interesado",
        "expediente": "Expediente",
        "radicado": "Radicado",
        "request_text": "Solicitud",
        "concept_text": "Concepto",
        "outcome_code": "Resultado",
        "page_number": "Página inicial",
        "end_page_number": "Página final",
    }
    st.dataframe(
        [
            {
                "Campo": field_labels.get(field, field),
                "Valor automático": automatic.get(field),
                "Valor vigente": record.get(field),
            }
            for field in REVIEW_FIELDS
        ],
        hide_index=True,
        use_container_width=True,
    )

with st.form(f"review_{record.get('decision_uid')}"):
    field_col1, field_col2 = st.columns(2)
    numeral = field_col1.text_input("Numeral", value=str(record.get("numeral") or ""))
    numeral_title = field_col2.text_input(
        "Título del numeral", value=str(record.get("numeral_title") or "")
    )
    session_date = field_col1.text_input(
        "Fecha de sesión (AAAA-MM-DD)", value=str(record.get("session_date") or "")
    )
    current_request_type = str(record.get("request_type_code") or "")
    request_type_options = ["", *REQUEST_TYPE_CODES]
    if current_request_type and current_request_type not in request_type_options:
        request_type_options.append(current_request_type)
    request_type = field_col2.selectbox(
        "Tipo de solicitud",
        request_type_options,
        index=request_type_options.index(current_request_type),
        format_func=lambda value: value.replace("_", " ").capitalize()
        if value
        else "Sin clasificar",
    )
    product = field_col1.text_input(
        "Producto", value=str(record.get("product_name") or "")
    )
    active = field_col2.text_input(
        "Principio activo", value=str(record.get("active_ingredient") or "")
    )
    interested = field_col1.text_input(
        "Interesado o titular", value=str(record.get("interested_party") or "")
    )
    expediente = field_col2.text_input(
        "Expediente", value=str(record.get("expediente") or "")
    )
    radicado = field_col1.text_input(
        "Radicado", value=str(record.get("radicado") or "")
    )
    current_outcome = str(record.get("outcome_code") or "sin_clasificar")
    outcome_options = list(OUTCOME_CODES)
    if current_outcome not in outcome_options:
        outcome_options.append(current_outcome)
    outcome = field_col2.selectbox(
        "Resultado normalizado",
        outcome_options,
        index=outcome_options.index(current_outcome),
        format_func=lambda value: value.replace("_", " ").capitalize(),
    )
    page_col1, page_col2 = st.columns(2)
    document_pages = max(
        1,
        int(record.get("pdf_page_count") or 0),
        int(record.get("page_number") or 1),
        int(record.get("end_page_number") or 1),
    )
    page_number = page_col1.number_input(
        "Página inicial",
        min_value=1,
        max_value=document_pages,
        value=int(record.get("page_number") or 1),
    )
    end_page = page_col2.number_input(
        "Página final",
        min_value=1,
        max_value=document_pages,
        value=int(record.get("end_page_number") or record.get("page_number") or 1),
    )
    request_text = st.text_area(
        "Solicitud", value=str(record.get("request_text") or ""), height=130
    )
    concept_text = st.text_area(
        "Concepto", value=str(record.get("concept_text") or ""), height=180
    )
    try:
        default_reviewer = str(st.user.email or "")
    except Exception:
        default_reviewer = ""
    reviewer = st.text_input(
        "Revisor declarado",
        value=default_reviewer or str(record.get("reviewer") or ""),
        help="Sin SSO este nombre es declarado y no constituye identidad auditada.",
    )
    notes = st.text_area(
        "Comentario de revisión",
        value=str(record.get("review_notes") or ""),
        placeholder="Explica brevemente la corrección o aprobación.",
    )
    target_status = st.radio(
        "Acción",
        ["reviewed", "approved", "reopened"],
        format_func=lambda value: {
            "reviewed": "Guardar como revisada",
            "approved": "Aprobar ficha",
            "reopened": "Reabrir y volver al valor automático",
        }[value],
        horizontal=True,
    )
    submitted = st.form_submit_button("Registrar revisión", type="primary")

if submitted:
    current_values = {
        "numeral": numeral,
        "numeral_title": numeral_title,
        "session_date": session_date,
        "request_type_code": request_type,
        "product_name": product,
        "active_ingredient": active,
        "interested_party": interested,
        "expediente": expediente,
        "radicado": radicado,
        "request_text": request_text,
        "concept_text": concept_text,
        "outcome_code": outcome,
        "page_number": int(page_number),
        "end_page_number": int(end_page),
    }
    corrections = {
        field: value
        for field, value in current_values.items()
        if value != automatic.get(field)
    }
    try:
        event = new_review_event(
            record,
            status=target_status,
            reviewer=reviewer,
            notes=notes,
            corrections={} if target_status == "reopened" else corrections,
        )
        events = append_review_event(REVIEW_LOG_PATH, event)
        st.session_state["review_saved_message"] = (
            "La revisión se registró. Para conservarla después de un reinicio, "
            "descarga el archivo y súbelo como data/regulatory-review-log.csv."
        )
        st.rerun()
    except ValueError as exc:
        st.error(str(exc))

st.divider()
st.subheader("Registro portable de revisiones")
st.download_button(
    "Descargar todas las revisiones",
    data=serialize_review_events(events).encode("utf-8-sig"),
    file_name="regulatory-review-log.csv",
    mime="text/csv",
)
uploaded = st.file_uploader(
    "Importar un registro de revisiones",
    type=["csv"],
    help="La importación reemplaza únicamente la copia temporal de revisiones.",
)
if uploaded is not None:
    try:
        imported_events = parse_review_events(uploaded.getvalue().decode("utf-8-sig"))
        if st.button("Usar registro importado"):
            write_review_events(imported_events, REVIEW_LOG_PATH)
            st.success(f"Se importaron {len(imported_events)} eventos.")
            st.rerun()
    except (UnicodeDecodeError, ValueError) as exc:
        st.error(f"No fue posible importar el archivo: {exc}")

history = [
    event
    for event in events
    if event.decision_uid == str(record.get("decision_uid") or "")
]
with st.expander(f"Historial de esta ficha ({len(history)})"):
    for event in reversed(history):
        st.markdown(
            f"**{event.status}** · {event.reviewer} · {event.reviewed_at}"
        )
        if event.notes:
            st.write(event.notes)
        if event.corrections:
            st.json(event.corrections)
