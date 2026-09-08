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
from services.ui_helpers import apply_app_style


st.set_page_config(page_title="Catálogo de actas", page_icon="📚", layout="wide")
apply_app_style()
st.title("Catálogo oficial de actas")
st.caption(
    "Explora el registro histórico publicado por la Sala Especializada de "
    "Medicamentos de Síntesis Química y Biológica y consulta su estado en el índice."
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

years = sorted({record.year for record in records}, reverse=True)
sections = sorted({record.section for record in records})
statuses = [
    "Indexada",
    "Pendiente de indexar",
    "Registrada (fuera del índice)",
    "Sin enlace",
]


def index_status(url: str, year: int) -> str:
    if not url:
        return "Sin enlace"
    if year < INDEX_START_YEAR:
        return "Registrada (fuera del índice)"
    if url in indexed_urls:
        return "Indexada"
    return "Pendiente de indexar"


currently_listed = sum(
    record.listing_status.startswith("listed") for record in records
)
pending = sum(
    index_status(record.url, record.year) == "Pendiente de indexar"
    for record in records
)
without_url = sum(not record.url for record in records)

st.subheader("Resumen del catálogo")
metric1, metric2, metric3, metric4 = st.columns(4)
metric1.metric("Registros históricos", len(records))
metric2.metric("Verificados en página", currently_listed)
metric3.metric("Pendientes de indexar", pending)
metric4.metric("Sin enlace", without_url)

if report and report.get("source_checked"):
    st.caption(
        f"Última comprobación de la fuente: "
        f"{report.get('generated_at', 'sin fecha')}"
    )
else:
    st.warning(
        "El catálogo inicial todavía no ha sido contrastado automáticamente con "
        "la página de INVIMA. Ejecuta el workflow Actualizar catálogo desde INVIMA."
    )


def clear_catalog_filters() -> None:
    st.session_state["catalog_query"] = ""
    st.session_state["catalog_years"] = []
    st.session_state["catalog_sections"] = []
    st.session_state["catalog_statuses"] = []


with st.container(border=True):
    st.subheader("Buscar y filtrar")
    search_col, reset_col = st.columns([4, 1], vertical_alignment="bottom")
    query = search_col.text_input(
        "Buscar en títulos",
        placeholder="Ejemplo: Acta 08 de 2026",
        key="catalog_query",
    ).strip().lower()
    reset_col.button(
        "Limpiar filtros",
        on_click=clear_catalog_filters,
        use_container_width=True,
    )

    filter_col1, filter_col2, filter_col3 = st.columns(3)
    selected_years = filter_col1.multiselect(
        "Año",
        years,
        key="catalog_years",
    )
    selected_sections = filter_col2.multiselect(
        "Serie o sala publicada",
        sections,
        key="catalog_sections",
    )
    selected_statuses = filter_col3.multiselect(
        "Estado en la aplicación",
        statuses,
        key="catalog_statuses",
    )

filtered = []
for record in records:
    status = index_status(record.url, record.year)
    if (
        query
        and query not in record.title.lower()
        and query not in record.published_title.lower()
    ):
        continue
    if selected_years and record.year not in selected_years:
        continue
    if selected_sections and record.section not in selected_sections:
        continue
    if selected_statuses and status not in selected_statuses:
        continue
    filtered.append((record, status))

table_rows = [
    {
        "Año": record.year,
        "Acta": record.acta_number,
        "Serie/sala": record.section,
        "Parte": record.part or "Completa o no indicada",
        "Fecha de publicación": record.publication_date or record.publication_date_raw,
        "Estado": status,
        "En fuente oficial": (
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

output = io.StringIO()
if table_rows:
    writer = csv.DictWriter(output, fieldnames=table_rows[0].keys())
    writer.writeheader()
    writer.writerows(table_rows)

results_tab, diagnostics_tab = st.tabs(["Actas", "Información del catálogo"])

with results_tab:
    result_header, download_col = st.columns([3, 1])
    result_header.subheader(f"Resultados ({len(table_rows)})")
    download_col.download_button(
        "Descargar CSV",
        data=output.getvalue().encode("utf-8-sig"),
        file_name="catalogo_actas_invima.csv",
        mime="text/csv",
        disabled=not table_rows,
        use_container_width=True,
    )
    if table_rows:
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
    else:
        st.info(
            "No hay actas que coincidan con los filtros actuales. Prueba otro "
            "término o limpia los filtros."
        )

with diagnostics_tab:
    st.link_button(
        "Abrir página oficial de INVIMA",
        CATALOG_SOURCE_URL,
    )
    if report and report.get("source_checked"):
        gaps = report.get("possible_number_gaps", [])
        shared_urls = report.get("shared_url_conflicts", [])
        with st.expander(
            f"Posibles saltos de numeración ({len(gaps)})",
            expanded=False,
        ):
            st.write(
                "Estas señales no implican que falte una publicación: INVIMA puede "
                "no utilizar números consecutivos en todas las series."
            )
            if gaps:
                st.dataframe(gaps, use_container_width=True, hide_index=True)
            else:
                st.success("No se detectaron saltos de numeración.")
        with st.expander(
            f"Enlaces compartidos por varias entradas ({len(shared_urls)})",
            expanded=False,
        ):
            if shared_urls:
                st.write(
                    "Estas entradas se conservan en el catálogo, pero el PDF se "
                    "indexa una sola vez mientras comparten el mismo enlace oficial."
                )
                st.dataframe(
                    shared_urls,
                    use_container_width=True,
                    hide_index=True,
                )
            else:
                st.success("No se detectaron enlaces compartidos.")
    else:
        st.info(
            "La información técnica estará disponible después de actualizar el "
            "catálogo desde la fuente oficial."
        )
