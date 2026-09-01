from __future__ import annotations

import streamlit as st

from config import (
    ACTAS_CATALOG_PATH,
    DATABASE_PATH,
    INTEGRITY_REPORT_PATH,
    ensure_directories,
)
from services.catalog import load_catalog
from services.database import database_stats
from services.integrity import load_integrity_report


st.set_page_config(
    page_title="Explorador de Actas INVIMA",
    page_icon="🔎",
    layout="wide",
)

ensure_directories()
stats = database_stats(DATABASE_PATH)
integrity = load_integrity_report(INTEGRITY_REPORT_PATH)
catalog = load_catalog(ACTAS_CATALOG_PATH)

st.title("Explorador de Actas INVIMA")
st.caption("Consulta documental y analista con IA basado en fuentes verificables")
st.caption(
    "Fuente documental: página oficial de la Sala Especializada de Medicamentos "
    "de Síntesis Química y Biológica"
)

col1, col2, col3, col4 = st.columns(4)
col1.metric("Actas registradas", len(catalog))
col2.metric("Documentos indexados", stats["documents"])
col3.metric("Páginas con texto", stats["pages"])
col4.metric("Fragmentos consultables", stats["chunks"])

if stats["documents"] == 0:
    st.warning(
        "Todavía no existe un índice. Ejecuta **Actions → Construir índice → "
        "Run workflow** en GitHub."
    )
elif integrity and integrity.get("status") == "error":
    st.error("El último informe de integridad detectó inconsistencias en el corpus.")
elif integrity and integrity.get("status") == "warning":
    st.warning(
        "El corpus está completo, pero el informe contiene advertencias "
        "documentales por revisar."
    )

st.subheader("Qué permite hacer")
st.markdown(
    """
- **Explorar actas:** buscar términos, frases, productos, principios activos,
  radicados y otros identificadores.
- **Filtrar resultados:** por año, número de acta, sala/sección y parte.
- **Analizar con IA:** formular preguntas y recibir respuestas sustentadas en
  fragmentos de las actas.
- **Verificar cada afirmación:** consultar título, página y enlace de origen de
  las fuentes utilizadas.
- **Comprobar el corpus:** revisar cobertura, consistencia y páginas candidatas
  para OCR desde la página Integridad.
- **Consultar el catálogo:** distinguir actas indexadas, pendientes, retiradas de
  la página y publicaciones sin enlace disponible.
"""
)

st.info(
    "Las respuestas automáticas son una ayuda documental. Verifica cada cita "
    "contra el acta oficial; cualquier respaldo no oficial aparece marcado "
    "explícitamente como copia histórica."
)
