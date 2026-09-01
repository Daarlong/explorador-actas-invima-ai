from __future__ import annotations

import streamlit as st

from config import DATABASE_PATH, ensure_directories
from services.database import database_stats


st.set_page_config(
    page_title="Explorador de Actas INVIMA",
    page_icon="🔎",
    layout="wide",
)

ensure_directories()
stats = database_stats(DATABASE_PATH)

st.title("Explorador de Actas INVIMA")
st.caption("Consulta documental y analista con IA basado en fuentes verificables")

col1, col2, col3 = st.columns(3)
col1.metric("Actas indexadas", stats["documents"])
col2.metric("Páginas con texto", stats["pages"])
col3.metric("Fragmentos consultables", stats["chunks"])

if stats["documents"] == 0:
    st.warning(
        "Todavía no existe un índice. Abre **Administración** para construirlo "
        "desde el manifiesto de documentos."
    )

st.subheader("Qué permite hacer")
st.markdown(
    """
- **Explorar actas:** buscar términos, frases, productos, principios activos,
  radicados y otros identificadores.
- **Filtrar resultados:** por año, número de acta, sala/sección y parte.
- **Analizar con IA:** formular preguntas y recibir respuestas sustentadas en
  fragmentos de las actas.
- **Verificar cada afirmación:** consultar título, página y enlace oficial de
  las fuentes utilizadas.
"""
)

st.info(
    "Las respuestas automáticas son una ayuda documental. La fuente oficial es "
    "siempre el acta publicada por INVIMA."
)

