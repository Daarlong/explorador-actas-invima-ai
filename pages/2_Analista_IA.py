from __future__ import annotations

import streamlit as st

from config import DATABASE_PATH, DEFAULT_TOP_K, MAX_CONTEXT_CHARS
from services.database import database_stats, get_chunks_by_ids, get_filter_options
from services.llm import generate_answer, load_llm_settings
from services.models import SearchResult
from services.pdf_viewer import viewer_session_values
from services.retrieval import (
    build_grounded_prompt,
    retrieve_evidence,
    select_context_results,
    validate_citations,
)
from services.ui_helpers import apply_app_style


st.set_page_config(page_title="Analista IA", page_icon="💬", layout="wide")
apply_app_style()
st.title("Analista IA")
st.caption(
    "Formula una pregunta, recupera evidencia del corpus y prepara una respuesta "
    "trazable hasta la página de cada acta."
)

if database_stats(DATABASE_PATH)["documents"] == 0:
    st.warning(
        "No existe un índice. Ejecuta primero Construir índice desde GitHub Actions."
    )
    st.stop()

try:
    secret_values = dict(st.secrets)
except Exception:
    secret_values = {}
settings = load_llm_settings(secret_values)

options = get_filter_options(DATABASE_PATH)
selected_source_dicts = st.session_state.get("analysis_selected_sources", [])
if "analysis_use_selected" not in st.session_state:
    st.session_state["analysis_use_selected"] = bool(selected_source_dicts)
with st.sidebar:
    st.subheader("Alcance de la consulta")
    st.caption("Acota las actas que se usarán para recuperar evidencia.")
    years = st.multiselect("Año", options["years"], key="ai_years")
    acta_numbers = st.multiselect(
        "Número de acta", options["acta_numbers"], key="ai_actas"
    )
    sections = st.multiselect("Sala o sección", options["sections"], key="ai_sections")
    parts = st.multiselect("Parte", options["parts"], key="ai_parts")
    top_k = st.slider("Fuentes para la respuesta", 4, 15, DEFAULT_TOP_K)
    retrieval_mode_label = st.radio(
        "Recuperación documental",
        ["Híbrida", "Textual", "Semántica"],
        disabled=bool(selected_source_dicts)
        and bool(st.session_state.get("analysis_use_selected")),
    )
    use_selected = st.checkbox(
        f"Usar fuentes seleccionadas ({len(selected_source_dicts)})",
        key="analysis_use_selected",
        disabled=not selected_source_dicts,
    )
    st.divider()
    if st.button(
        "Quitar fuentes seleccionadas",
        use_container_width=True,
        disabled=not selected_source_dicts,
    ):
        st.session_state["analysis_selected_sources"] = []
        st.session_state["analysis_use_selected"] = False
        st.rerun()

    if st.button(
        "Limpiar conversación",
        use_container_width=True,
        disabled=not st.session_state.get("chat_messages"),
    ):
        st.session_state["chat_messages"] = []
        st.rerun()

    st.divider()
    if settings.is_configured:
        st.success(f"Proveedor configurado: {settings.provider}")
    else:
        st.info(
            "Modo de preparación: la aplicación no envía información a una IA "
            "externa."
        )

if "chat_messages" not in st.session_state:
    st.session_state.chat_messages = []

if selected_source_dicts and use_selected:
    st.success(
        f"Consulta acotada a {len(selected_source_dicts)} evidencias seleccionadas "
        "en el Explorador."
    )
elif not settings.is_configured:
    st.info(
        "La aplicación recuperará las fuentes y generará un contexto listo para "
        "copiar en el modelo corporativo aprobado."
    )


def render_sources(sources: list[dict], *, key_prefix: str) -> None:
    if not sources:
        return
    with st.expander(f"Fuentes consultadas ({len(sources)})", expanded=False):
        for source_index, source in enumerate(sources, start=1):
            source_note = (
                " · copia histórica"
                if source.get("source_type") == "historical_mirror"
                else ""
            )
            with st.container(border=True):
                st.markdown(
                    f"**[F{source_index}] {source['title']}**  "
                    f"\nPágina {source['page']}{source_note}"
                )
                st.write(source["text"])
                source_actions = st.columns(2)
                link_label = (
                    "Abrir copia histórica"
                    if source.get("source_type") == "historical_mirror"
                    else "Abrir PDF oficial"
                )
                source_actions[0].link_button(
                    link_label,
                    f"{source['url']}#page={source['page']}",
                    key=f"open_ai_source_{key_prefix}_{source_index}",
                    use_container_width=True,
                )
                if source_actions[1].button(
                    "Ver en el visor",
                    key=f"view_ai_source_{key_prefix}_{source_index}",
                    use_container_width=True,
                ):
                    st.session_state.update(
                        viewer_session_values(
                            source,
                            origin_page="pages/2_Analista_IA.py",
                        )
                    )
                    st.switch_page("pages/1_Explorador.py")


if not st.session_state.chat_messages:
    with st.container(border=True):
        st.subheader("Empieza con una pregunta concreta")
        st.caption(
            "Puedes consultar todo el corpus o seleccionar primero fuentes en el "
            "Explorador. La respuesta siempre debe contrastarse con las actas."
        )
        example_col1, example_col2, example_col3 = st.columns(3)
        example_col1.markdown(
            "**Buscar antecedentes**  \n"
            "¿Qué conceptos se han emitido sobre semaglutida?"
        )
        example_col2.markdown(
            "**Resumir una decisión**  \n"
            "Resume la solicitud y el concepto de esta acta."
        )
        example_col3.markdown(
            "**Comparar criterios**  \n"
            "¿Qué diferencias hay entre estas fuentes seleccionadas?"
        )


for message_index, message in enumerate(st.session_state.chat_messages):
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if message.get("prompt"):
            st.code(message["prompt"], language="text")
        render_sources(
            message.get("sources", []),
            key_prefix=f"history_{message_index}",
        )

question = st.chat_input("Pregunta sobre las actas...")
if question:
    st.session_state.chat_messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    filters = {
        "years": years,
        "acta_numbers": acta_numbers,
        "sections": sections,
        "parts": parts,
    }
    with st.spinner("Buscando evidencia en las actas..."):
        if use_selected and selected_source_dicts:
            selected_ids = [
                int(source["chunk_id"])
                for source in selected_source_dicts
                if source.get("chunk_id") is not None
            ]
            current_results = get_chunks_by_ids(DATABASE_PATH, selected_ids)
            current_by_id = {result.chunk_id: result for result in current_results}
            results: list[SearchResult] = []
            for stored in selected_source_dicts:
                current = current_by_id.get(int(stored.get("chunk_id", -1)))
                if (
                    current
                    and current.title == stored.get("title")
                    and current.page == stored.get("page")
                    and current.text == stored.get("text")
                ):
                    results.append(current)
        else:
            retrieval_modes = {
                "Híbrida": "hybrid",
                "Textual": "textual",
                "Semántica": "semantic",
            }
            results = retrieve_evidence(
                DATABASE_PATH,
                question,
                top_k=top_k,
                filters=filters,
                mode=retrieval_modes[retrieval_mode_label],
            )

    with st.chat_message("assistant"):
        if not results:
            answer = "No encontré evidencia suficiente en las actas seleccionadas."
            st.warning(answer)
            st.session_state.chat_messages.append(
                {"role": "assistant", "content": answer, "sources": []}
            )
        else:
            results = select_context_results(
                results,
                max_chars=MAX_CONTEXT_CHARS,
            )
            prompt = build_grounded_prompt(
                question,
                results,
                max_context_chars=MAX_CONTEXT_CHARS,
            )
            sources = [result.as_dict() for result in results]
            if settings.is_configured:
                try:
                    with st.spinner("Generando respuesta sustentada..."):
                        answer = generate_answer(prompt, settings)
                except Exception as exc:
                    answer = "No fue posible consultar el proveedor de IA."
                    st.error(f"{answer} Detalle técnico: {exc}")
                    st.code(prompt, language="text")
                    stored_message = {
                        "role": "assistant",
                        "content": answer,
                        "prompt": prompt,
                        "sources": sources,
                    }
                else:
                    st.markdown(answer)
                    citations_are_valid, invalid_citations = validate_citations(
                        answer, len(sources)
                    )
                    if not citations_are_valid:
                        detail = (
                            f" Referencias inexistentes: {invalid_citations}."
                            if invalid_citations
                            else " La respuesta no contiene referencias [F#]."
                        )
                        st.error(
                            "La validación automática de citas falló; no uses la "
                            f"respuesta sin revisar las fuentes.{detail}"
                        )
                    stored_message = {
                        "role": "assistant",
                        "content": answer,
                        "sources": sources,
                    }
                render_sources(
                    sources,
                    key_prefix=f"history_{len(st.session_state.chat_messages)}",
                )
                st.session_state.chat_messages.append(stored_message)
            else:
                answer = (
                    "La evidencia está lista. Copia el siguiente contexto en el "
                    "LLM corporativo aprobado."
                )
                st.info(answer)
                st.code(prompt, language="text")
                render_sources(
                    sources,
                    key_prefix=f"history_{len(st.session_state.chat_messages)}",
                )
                st.session_state.chat_messages.append(
                    {
                        "role": "assistant",
                        "content": answer,
                        "prompt": prompt,
                        "sources": sources,
                    }
                )

st.divider()
st.caption(
    "Las respuestas y los contextos son ayudas de consulta. Verifica siempre cada "
    "afirmación contra las páginas citadas."
)
