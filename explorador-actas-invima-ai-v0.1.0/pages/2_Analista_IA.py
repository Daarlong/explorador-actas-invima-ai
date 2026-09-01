from __future__ import annotations

import streamlit as st

from config import DATABASE_PATH, DEFAULT_TOP_K, MAX_CONTEXT_CHARS
from services.database import database_stats, get_filter_options
from services.llm import generate_answer, load_llm_settings
from services.retrieval import (
    build_grounded_prompt,
    retrieve_evidence,
    validate_citations,
)


st.set_page_config(page_title="Analista IA", page_icon="💬", layout="wide")
st.title("💬 Analista IA")
st.caption("Respuestas fundamentadas en las actas recuperadas")

if database_stats(DATABASE_PATH)["documents"] == 0:
    st.warning("No existe un índice. Constrúyelo primero desde Administración.")
    st.stop()

try:
    secret_values = dict(st.secrets)
except Exception:
    secret_values = {}
settings = load_llm_settings(secret_values)

options = get_filter_options(DATABASE_PATH)
with st.sidebar:
    st.header("Alcance del análisis")
    years = st.multiselect("Año", options["years"], key="ai_years")
    acta_numbers = st.multiselect(
        "Número de acta", options["acta_numbers"], key="ai_actas"
    )
    sections = st.multiselect("Sala o sección", options["sections"], key="ai_sections")
    parts = st.multiselect("Parte", options["parts"], key="ai_parts")
    top_k = st.slider("Fuentes para la respuesta", 4, 15, DEFAULT_TOP_K)

    if settings.is_configured:
        st.success(f"IA configurada: {settings.provider}")
    else:
        st.info("Modo seguro: preparación de contexto sin enviar datos a una IA externa")

if "chat_messages" not in st.session_state:
    st.session_state.chat_messages = []


def render_sources(sources: list[dict]) -> None:
    if not sources:
        return
    with st.expander("Fuentes recuperadas", expanded=False):
        for source_index, source in enumerate(sources, start=1):
            st.markdown(
                f"**[F{source_index}] {source['title']} — página {source['page']}**"
            )
            st.write(source["text"])
            st.markdown(
                f"[Abrir documento oficial en la página {source['page']}]"
                f"({source['url']}#page={source['page']})"
            )


for message in st.session_state.chat_messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])
        if message.get("prompt"):
            st.code(message["prompt"], language="text")
        render_sources(message.get("sources", []))

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
        results = retrieve_evidence(
            DATABASE_PATH,
            question,
            top_k=top_k,
            filters=filters,
        )

    with st.chat_message("assistant"):
        if not results:
            answer = "No encontré evidencia suficiente en las actas seleccionadas."
            st.warning(answer)
            st.session_state.chat_messages.append(
                {"role": "assistant", "content": answer, "sources": []}
            )
        else:
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
                    render_sources(sources)
                    st.session_state.chat_messages.append(
                        {
                            "role": "assistant",
                            "content": answer,
                            "sources": sources,
                        }
                    )
                except Exception as exc:
                    answer = "No fue posible consultar el proveedor de IA."
                    st.error(f"{answer} Detalle técnico: {exc}")
                    st.code(prompt, language="text")
                    render_sources(sources)
                    st.session_state.chat_messages.append(
                        {
                            "role": "assistant",
                            "content": answer,
                            "prompt": prompt,
                            "sources": sources,
                        }
                    )
            else:
                answer = (
                    "La evidencia está lista. Copia el siguiente contexto en el "
                    "LLM corporativo aprobado."
                )
                st.info(answer)
                st.code(prompt, language="text")
                render_sources(sources)
                st.session_state.chat_messages.append(
                    {
                        "role": "assistant",
                        "content": answer,
                        "prompt": prompt,
                        "sources": sources,
                    }
                )

st.caption(
    "Verifica siempre la respuesta contra las páginas citadas del documento oficial."
)
