from __future__ import annotations

import streamlit as st

from config import INDEXING_REPORT_PATH, INTEGRITY_REPORT_PATH
from services.indexing import load_indexing_report
from services.integrity import load_integrity_report


st.set_page_config(page_title="Integridad del corpus", page_icon="✅", layout="wide")
st.title("✅ Integridad del corpus")
st.caption("Cobertura, consistencia del índice y páginas que podrían requerir OCR")

report = load_integrity_report(INTEGRITY_REPORT_PATH)
indexing_report = load_indexing_report(INDEXING_REPORT_PATH)
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

st.subheader("Cobertura del índice")
coverage_by_year = report.get("coverage_by_year", [])
if coverage_by_year:
    st.dataframe(
        [
            {
                "Año": item.get("year"),
                "Documentos esperados": item.get("manifest_documents", 0),
                "Documentos indexados": item.get("indexed_documents", 0),
                "Pendientes": item.get("missing_documents", 0),
                "Cobertura": f"{item.get('coverage_percent', 0)} %",
            }
            for item in coverage_by_year
        ],
        use_container_width=True,
        hide_index=True,
    )

coverage_by_section = report.get("coverage_by_section", [])
with st.expander("Cobertura por serie o sala", expanded=False):
    if coverage_by_section:
        st.dataframe(
            [
                {
                    "Serie/sala": item.get("section"),
                    "Esperados": item.get("manifest_documents", 0),
                    "Indexados": item.get("indexed_documents", 0),
                    "Pendientes": item.get("missing_documents", 0),
                    "Cobertura": f"{item.get('coverage_percent', 0)} %",
                }
                for item in coverage_by_section
            ],
            use_container_width=True,
            hide_index=True,
        )
    else:
        st.write("No hay datos de cobertura por serie.")

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

if indexing_report:
    st.subheader("Última actualización del índice")
    run_col1, run_col2, run_col3, run_col4 = st.columns(4)
    run_col1.metric(
        "Documentos incorporados",
        indexing_report.get("documents_indexed", 0),
    )
    run_col2.metric(
        "Documentos fallidos",
        indexing_report.get("documents_failed", 0),
    )
    run_col3.metric(
        "Páginas recuperadas con OCR",
        indexing_report.get("ocr_pages_indexed", 0),
    )
    alternate_links = indexing_report.get("alternate_links_used") or []
    run_col4.metric("Enlaces alternativos usados", len(alternate_links))

    feature_col1, feature_col2, feature_col3 = st.columns(3)
    feature_col1.metric(
        "Documentos estructurados en esta ejecución",
        indexing_report.get("regulatory_documents_processed", 0),
    )
    feature_col2.metric(
        "Registros extraídos en esta ejecución",
        indexing_report.get("regulatory_records_extracted", 0),
    )
    feature_col3.metric(
        "Índice semántico",
        indexing_report.get("semantic_index_status", "sin ejecutar"),
    )

    indexing_errors = indexing_report.get("errors") or []
    if indexing_errors:
        with st.expander(
            f"Errores de descarga o procesamiento ({len(indexing_errors)})",
            expanded=True,
        ):
            st.write(
                "Estos documentos permanecen pendientes y el proceso automático "
                "volverá a intentarlos."
            )
            for error in indexing_errors:
                st.write(f"- {error}")

    if alternate_links:
        with st.expander(
            f"Enlaces alternativos utilizados ({len(alternate_links)})",
            expanded=False,
        ):
            st.dataframe(
                [
                    {
                        "Documento": item.get("title"),
                        "Enlace registrado": item.get("manifest_url"),
                        "Enlace utilizado": item.get("used_url"),
                    }
                    for item in alternate_links
                ],
                use_container_width=True,
                hide_index=True,
                column_config={
                    "Enlace registrado": st.column_config.LinkColumn(
                        "Enlace registrado",
                        display_text="Abrir",
                    ),
                    "Enlace utilizado": st.column_config.LinkColumn(
                        "Enlace utilizado",
                        display_text="Abrir",
                    ),
                },
            )
    st.caption(
        "Última ejecución del índice: "
        f"{indexing_report.get('generated_at', 'sin fecha')}"
    )

regulatory_pending = report.get("regulatory_extraction_pending", []) or []
regulatory_errors = report.get("regulatory_extraction_errors", []) or []
if regulatory_pending or regulatory_errors:
    with st.expander(
        "Extracción de campos regulatorios",
        expanded=bool(regulatory_errors),
    ):
        if regulatory_pending:
            st.write(
                f"Documentos pendientes de extracción: {len(regulatory_pending)}"
            )
            for title in regulatory_pending:
                st.write(f"- {title}")
        if regulatory_errors:
            st.write("Errores detectados:")
            st.dataframe(regulatory_errors, hide_index=True, use_container_width=True)

st.caption(
    f"Generado: {report.get('generated_at', 'sin fecha')} · "
    f"Esquema: {report.get('schema_version', 'desconocido')}"
)
