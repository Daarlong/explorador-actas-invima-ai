from __future__ import annotations

from pathlib import Path

import streamlit as st

from config import (
    ACTAS_CATALOG_PATH,
    CATALOG_REPORT_PATH,
    DATABASE_PATH,
    INDEXING_REPORT_PATH,
    INTEGRITY_REPORT_PATH,
    SEMANTIC_INDEX_PATH,
    SEMANTIC_REPORT_PATH,
    SOURCE_SNAPSHOT_PATH,
    ensure_directories,
)
from services.catalog import load_catalog
from services.database import dashboard_summary
from services.evaluation import audit_corpus_reports
from services.integrity import load_integrity_report
from services.semantic import semantic_index_status


st.set_page_config(
    page_title="Explorador de Actas INVIMA",
    page_icon="🔎",
    layout="wide",
)

ensure_directories()
summary = dashboard_summary(DATABASE_PATH)
integrity = load_integrity_report(INTEGRITY_REPORT_PATH) or {}
catalog = load_catalog(ACTAS_CATALOG_PATH)
semantic = semantic_index_status(SEMANTIC_INDEX_PATH)
audit = audit_corpus_reports(
    INTEGRITY_REPORT_PATH,
    INDEXING_REPORT_PATH,
    SEMANTIC_REPORT_PATH,
    catalog_report_path=CATALOG_REPORT_PATH,
    source_snapshot_path=SOURCE_SNAPSHOT_PATH,
)
version_path = Path(__file__).with_name("VERSION")
version = version_path.read_text(encoding="utf-8").strip() if version_path.exists() else ""

st.title("Explorador de Actas INVIMA")
st.caption(
    "Sala Especializada de Medicamentos de Síntesis Química y Biológica · "
    f"versión {version or 'sin identificar'}"
)

status = integrity.get("status")
if summary["documents"] == 0:
    st.error("El índice documental todavía no está disponible.")
elif status == "error" or audit.get("status") == "error":
    st.warning(
        "El índice puede consultarse, pero la auditoría detectó cobertura, "
        "integridad o fuente oficial pendiente de verificar."
    )
elif status == "warning" or audit.get("status") != "ok":
    st.warning("El corpus está disponible con advertencias documentales por revisar.")
else:
    st.success("El corpus está disponible para consulta.")

with st.form("quick_search"):
    search_col, button_col = st.columns([5, 1])
    quick_query = search_col.text_input(
        "Búsqueda rápida",
        placeholder=(
            "Producto, principio activo, interesado, expediente, radicado o concepto"
        ),
        label_visibility="collapsed",
    )
    quick_submit = button_col.form_submit_button(
        "Explorar",
        type="primary",
        use_container_width=True,
    )
if quick_submit and quick_query.strip():
    st.session_state["explorer_query"] = quick_query.strip()
    st.switch_page("pages/1_Explorador.py")

range_label = "Sin datos"
if summary["minimum_year"] and summary["maximum_year"]:
    range_label = f"{summary['minimum_year']}–{summary['maximum_year']}"
coverage = 0.0
manifest_documents = int(integrity.get("manifest_documents", 0) or 0)
indexed_documents = int(integrity.get("indexed_documents", 0) or 0)
if manifest_documents:
    coverage = round((indexed_documents / manifest_documents) * 100, 1)

metric1, metric2, metric3, metric4, metric5, metric6 = st.columns(6)
metric1.metric("Actas únicas", summary["unique_acts"])
metric2.metric("PDF/partes", summary["documents"])
metric3.metric("Páginas", summary["pages"])
metric4.metric("Registros extraídos", summary["regulatory_records"])
metric5.metric("Manifiesto → índice", f"{coverage} %")
metric6.metric("Rango real", range_label)

pending_documents = len(integrity.get("missing_documents", []) or [])
ocr_pages = sum(
    len(item.get("pages", []))
    for item in integrity.get("ocr_candidates", []) or []
)
semantic_label = "Disponible" if semantic.get("available") else "Pendiente"

status_col1, status_col2, status_col3, status_col4 = st.columns(4)
status_col1.metric("Catálogo histórico", len(catalog))
status_col2.metric("Pendientes de índice", pending_documents)
status_col3.metric("Páginas candidatas OCR", ocr_pages)
status_col4.metric("Búsqueda semántica", semantic_label)

dashboard_col, activity_col = st.columns([1.25, 1], gap="large")
with dashboard_col:
    st.subheader("Cobertura por año")
    coverage_rows = integrity.get("coverage_by_year", []) or []
    if coverage_rows:
        chart_data = {
            "Año": [str(item.get("year")) for item in reversed(coverage_rows)],
            "Documentos indexados": [
                int(item.get("indexed_documents", 0))
                for item in reversed(coverage_rows)
            ],
        }
        st.bar_chart(
            chart_data,
            x="Año",
            y="Documentos indexados",
            horizontal=False,
        )
        st.dataframe(
            [
                {
                    "Año": item.get("year"),
                    "Esperados": item.get("manifest_documents", 0),
                    "Indexados": item.get("indexed_documents", 0),
                    "Pendientes": item.get("missing_documents", 0),
                    "Cobertura": f"{item.get('coverage_percent', 0)} %",
                }
                for item in coverage_rows
            ],
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.info(
            "Ejecuta Construir índice con esta versión para generar el tablero "
            "de cobertura."
        )

with activity_col:
    st.subheader("Últimos documentos indexados")
    latest = summary.get("latest_documents", [])
    if latest:
        st.dataframe(
            [
                {
                    "Año": item.get("year"),
                    "Acta": item.get("acta_number"),
                    "Sala": item.get("section"),
                    "Parte": item.get("part") or "Completa",
                    "Documento": item.get("url"),
                }
                for item in latest
            ],
            hide_index=True,
            use_container_width=True,
            column_config={
                "Documento": st.column_config.LinkColumn(
                    "Documento",
                    display_text="Abrir",
                )
            },
        )
    else:
        st.write("No hay actividad de indexación registrada.")

    st.subheader("Estado de capacidades")
    st.write("✅ Búsqueda textual FTS5")
    st.write(
        ("✅" if semantic.get("available") else "⏳")
        + " Búsqueda semántica local"
    )
    st.write(
        ("✅" if summary["regulatory_records"] else "⏳")
        + " Campos regulatorios estructurados"
    )
    st.write("✅ Visor de página y selección de evidencia")
    st.write(
        ("✅" if audit.get("status") == "ok" else "⚠️")
        + " Auditoría reproducible del corpus"
    )

st.subheader("Accesos")
link1, link2, link3, link4 = st.columns(4)
link1.page_link("pages/1_Explorador.py", label="Abrir Explorador", icon="🔍")
link2.page_link("pages/2_Analista_IA.py", label="Abrir Analista", icon="💬")
link3.page_link("pages/7_Comparar.py", label="Comparar decisiones", icon="⚖️")
link4.page_link("pages/6_Evaluacion.py", label="Evaluar búsquedas", icon="📊")
link5, link6, link7, _ = st.columns(4)
link5.page_link("pages/8_Revision_Fichas.py", label="Revisar fichas", icon="📝")
link6.page_link("pages/4_Integridad.py", label="Ver Integridad", icon="✅")
link7.page_link("pages/5_Catalogo.py", label="Ver Catálogo", icon="📚")

st.info(
    "Las fichas regulatorias y las asociaciones semánticas son ayudas de "
    "recuperación documental. Verifica siempre el texto y la página del acta "
    "antes de utilizarlas."
)

st.caption(
    "Última indexación: "
    f"{summary.get('last_indexed_at') or 'sin registro'} · "
    "último informe de integridad: "
    f"{integrity.get('generated_at', 'sin registro')}"
)
