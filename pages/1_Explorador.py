from __future__ import annotations

import streamlit as st

from config import DATABASE_PATH
from services.database import database_stats, get_filter_options, search_chunks


st.set_page_config(page_title="Explorador de actas", page_icon="🔍", layout="wide")
st.title("🔍 Explorador de actas")
st.caption("Búsqueda textual con filtros y referencia exacta de página")

stats = database_stats(DATABASE_PATH)
if stats["documents"] == 0:
    st.warning(
        "No existe un índice. Ejecuta primero Construir índice desde GitHub Actions."
    )
    st.stop()

options = get_filter_options(DATABASE_PATH)
with st.sidebar:
    st.header("Filtros")
    years = st.multiselect("Año", options["years"])
    acta_numbers = st.multiselect("Número de acta", options["acta_numbers"])
    sections = st.multiselect("Sala o sección", options["sections"])
    parts = st.multiselect("Parte", options["parts"])
    top_k = st.slider("Máximo de resultados", 5, 40, 15)

query = st.text_input(
    "Consulta",
    placeholder="Ej.: semaglutida, registro sanitario, expediente 12345...",
)

if query:
    filters = {
        "years": years,
        "acta_numbers": acta_numbers,
        "sections": sections,
        "parts": parts,
    }
    results = search_chunks(DATABASE_PATH, query, top_k=top_k, filters=filters)

    if not results:
        st.warning("No se encontraron coincidencias con los filtros seleccionados.")
    else:
        st.success(f"{len(results)} fragmentos relevantes")
        for position, result in enumerate(results, start=1):
            with st.container(border=True):
                st.markdown(f"#### {position}. {result.title}")
                metadata = [f"Página {result.page}"]
                if result.section:
                    metadata.append(result.section)
                if result.part:
                    metadata.append(result.part)
                if result.source_type == "historical_mirror":
                    metadata.append("Copia histórica")
                st.caption(" · ".join(metadata))
                st.write(result.text)
                link_label = (
                    "Abrir copia histórica en la página"
                    if result.source_type == "historical_mirror"
                    else "Abrir documento oficial en la página"
                )
                st.markdown(
                    f"[{link_label} {result.page}]"
                    f"({result.url}#page={result.page})"
                )
else:
    st.info("Escribe una consulta para explorar las actas indexadas.")
