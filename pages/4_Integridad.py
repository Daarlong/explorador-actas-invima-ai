from __future__ import annotations

import streamlit as st

from config import INTEGRITY_REPORT_PATH
from services.integrity import load_integrity_report


st.set_page_config(page_title="Integridad del corpus", page_icon="✅", layout="wide")
st.title("✅ Integridad del corpus")
st.caption("Cobertura, consistencia del índice y páginas que podrían requerir OCR")

report = load_integrity_report(INTEGRITY_REPORT_PATH)
if not report:
    st.info(
        "Todavía no existe un informe. Ejecuta Actions → Construir índice → "
        "Run workflow en GitHub."
    )
    st.stop()

status = report.get("status")
if status == "ok":
    st.success("El catálogo y el índice pasaron todas las verificaciones.")
elif status == "warning":
    st.warning(
        "El índice está completo, pero existen advertencias documentales por revisar."
    )
else:
    st.error("El informe detectó inconsistencias que deben corregirse.")

col1, col2, col3, col4 = st.columns(4)
col1.metric("Manifiesto", report.get("manifest_documents", 0))
col2.metric("Indexados", report.get("indexed_documents", 0))
col3.metric("Páginas con texto", report.get("pages_indexed", 0))
col4.metric("Fragmentos", report.get("chunks", 0))

col5, col6, col7 = st.columns(3)
col5.metric("Tamaño SQLite", f"{report.get('database_mib', 0)} MiB")
col6.metric(
    "Cobertura de páginas",
    f"{report.get('text_page_coverage_percent', 0)} %",
)
col7.metric("Integridad SQLite", report.get("sqlite_integrity", "desconocida"))

problems = {
    "Documentos faltantes": report.get("missing_documents", []),
    "Documentos inesperados": report.get("unexpected_documents", []),
    "Documentos sin páginas": report.get("documents_without_pages", []),
    "Documentos sin fragmentos": report.get("documents_without_chunks", []),
}
for label, values in problems.items():
    if values:
        with st.expander(f"{label} ({len(values)})", expanded=True):
            for value in values:
                st.write(f"- {value}")

ocr_candidates = report.get("ocr_candidates", [])
page_inventory_pending = report.get("page_inventory_pending", [])
if page_inventory_pending:
    with st.expander(
        "Inventario de páginas pendiente "
        f"({len(page_inventory_pending)} documentos migrados)",
        expanded=False,
    ):
        st.write(
            "Estos documentos se migraron desde el índice anterior sin volver a "
            "descargar sus PDF. Una reconstrucción completa permite verificar también "
            "sus páginas finales sin texto."
        )
        for title in page_inventory_pending:
            st.write(f"- {title}")

with st.expander(
    f"Páginas sin texto o candidatas para OCR ({len(ocr_candidates)} documentos)",
    expanded=False,
):
    if ocr_candidates:
        st.dataframe(
            [
                {
                    "Documento": item.get("title"),
                    "Páginas": ", ".join(str(page) for page in item.get("pages", [])),
                }
                for item in ocr_candidates
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.write("No se detectaron páginas sin texto.")

st.caption(
    f"Generado: {report.get('generated_at', 'sin fecha')} · "
    f"Esquema: {report.get('schema_version', 'desconocido')}"
)
