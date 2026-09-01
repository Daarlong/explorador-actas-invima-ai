from __future__ import annotations

import csv
import io

import streamlit as st

from config import (
    ACTAS_CATALOG_PATH,
    CATALOG_REPORT_PATH,
    CATALOG_SOURCE_URL,
    DATABASE_PATH,
    INDEX_START_YEAR,
)
from services.catalog import load_catalog, load_catalog_report
from services.database import indexed_document_catalog


st.set_page_config(page_title="Catálogo de actas", page_icon="📚", layout="wide")
st.title("📚 Catálogo oficial de actas")
st.caption(
    "Registro histórico de las actas publicadas en la página oficial de la "
    "Sala Especializada de Medicamentos de Síntesis Química y Biológica."
)

records = load_catalog(ACTAS_CATALOG_PATH)
if not records:
    st.info(
        "Todavía no existe el catálogo. Ejecuta Actions → Actualizar catálogo "
        "desde INVIMA → Run workflow en GitHub."
    )
    st.stop()

indexed_urls = set(indexed_document_catalog(DATABASE_PATH))
report = load_catalog_report(CATALOG_REPORT_PATH)

query = st.text_input(
    "Buscar en títulos",
    placeholder="Ejemplo: Acta 08 de 2026",
).strip().lower()

years = sorted({record.year for record in records}, reverse=True)
sections = sorted({record.section for record in records})
statuses = [
    "Indexada",
    "Pendiente de indexar",
    "Registrada (fuera del índice)",
    "Sin enlace",
]

filter_col1, filter_col2, filter_col3 = st.columns(3)
selected_years = filter_col1.multiselect("Año", years)
selected_sections = filter_col2.multiselect("Serie o sala publicada", sections)
selected_statuses = filter_col3.multiselect("Estado", statuses)


def index_status(url: str, year: int) -> str:
    if not url:
        return "Sin enlace"
    if year < INDEX_START_YEAR:
        return "Registrada (fuera del índice)"
    if url in indexed_urls:
        return "Indexada"
    return "Pendiente de indexar"


filtered = []
for record in records:
    status = index_status(record.url, record.year)
    if query and query not in record.title.lower() and query not in record.published_title.lower():
        continue
    if selected_years and record.year not in selected_years:
        continue
    if selected_sections and record.section not in selected_sections:
        continue
    if selected_statuses and status not in selected_statuses:
        continue
    filtered.append((record, status))

currently_listed = sum(
    record.listing_status.startswith("listed") for record in records
)
pending = sum(
    index_status(record.url, record.year) == "Pendiente de indexar"
    for record in records
)
without_url = sum(not record.url for record in records)

metric1, metric2, metric3, metric4 = st.columns(4)
metric1.metric("Registros históricos", len(records))
metric2.metric("Verificados en página", currently_listed)
metric3.metric("Pendientes de indexar", pending)
metric4.metric("Sin enlace", without_url)

if report and report.get("source_checked"):
    st.caption(
        f"Última revisión: {report.get('generated_at', 'sin fecha')} · "
        f"Fuente: {CATALOG_SOURCE_URL}"
    )
else:
    st.warning(
        "El catálogo inicial todavía no ha sido contrastado automáticamente con "
        "la página de INVIMA. Ejecuta el workflow Actualizar catálogo desde INVIMA."
    )

table_rows = [
    {
        "Año": record.year,
        "Acta": record.acta_number,
        "Serie/sala": record.section,
        "Parte": record.part or "Completa o no indicada",
        "Fecha de publicación": record.publication_date or record.publication_date_raw,
        "Estado": status,
        "En página": (
            "Sí"
            if record.listing_status.startswith("listed")
            else (
                "Pendiente de verificar"
                if record.listing_status == "known"
                else "Histórica/no listada"
            )
        ),
        "Título": record.title,
        "Documento": record.url,
    }
    for record, status in filtered
]

st.write(f"Mostrando **{len(table_rows)}** registros")
st.dataframe(
    table_rows,
    use_container_width=True,
    hide_index=True,
    column_config={
        "Documento": st.column_config.LinkColumn(
            "Documento oficial",
            display_text="Abrir",
        ),
        "Año": st.column_config.NumberColumn(format="%d"),
    },
)

output = io.StringIO()
if table_rows:
    writer = csv.DictWriter(output, fieldnames=table_rows[0].keys())
    writer.writeheader()
    writer.writerows(table_rows)
st.download_button(
    "Descargar catálogo filtrado (CSV)",
    data=output.getvalue().encode("utf-8-sig"),
    file_name="catalogo_actas_invima.csv",
    mime="text/csv",
    disabled=not table_rows,
)

if report and report.get("source_checked"):
    gaps = report.get("possible_number_gaps", [])
    shared_urls = report.get("shared_url_conflicts", [])
    with st.expander(
        f"Posibles saltos de numeración ({len(gaps)})",
        expanded=False,
    ):
        st.write(
            "Son señales para revisión, no una afirmación de que falte una publicación: "
            "INVIMA puede no utilizar números consecutivos en todas las series."
        )
        if gaps:
            st.dataframe(gaps, use_container_width=True, hide_index=True)
        else:
            st.write("No se detectaron saltos de numeración.")
    if shared_urls:
        with st.expander(
            f"Enlaces compartidos por varias entradas ({len(shared_urls)})",
            expanded=False,
        ):
            st.write(
                "Estas entradas se conservan en el catálogo, pero el PDF se indexa "
                "una sola vez hasta confirmar si el enlace oficial es correcto."
            )
            st.dataframe(shared_urls, use_container_width=True, hide_index=True)
