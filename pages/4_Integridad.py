from __future__ import annotations

import streamlit as st

from config import (
    CATALOG_REPORT_PATH,
    DATABASE_PATH,
    INDEXING_REPORT_PATH,
    INTEGRITY_REPORT_PATH,
    SEMANTIC_INDEX_PATH,
    SEMANTIC_REPORT_PATH,
    SOURCE_SNAPSHOT_PATH,
)
from services.corpus_status import corpus_status_from_reports
from services.indexing import load_indexing_report
from services.integrity import load_integrity_report
from services.semantic import semantic_index_status
from services.ui_helpers import apply_app_style


st.set_page_config(page_title="Integridad del corpus", page_icon="✅", layout="wide")
apply_app_style()
st.title("Integridad del corpus")
st.caption(
    "Comprueba la cobertura documental, la consistencia del índice y la "
    "disponibilidad de los campos estructurados."
)

report = load_integrity_report(INTEGRITY_REPORT_PATH)
indexing_report = load_indexing_report(INDEXING_REPORT_PATH)
semantic_state = semantic_index_status(
    SEMANTIC_INDEX_PATH,
    source_database_path=DATABASE_PATH,
)
corpus_status = corpus_status_from_reports(
    INTEGRITY_REPORT_PATH,
    INDEXING_REPORT_PATH,
    SEMANTIC_REPORT_PATH,
    catalog_report_path=CATALOG_REPORT_PATH,
    source_snapshot_path=SOURCE_SNAPSHOT_PATH,
)
if not report:
    st.info(
        "Todavía no existe un informe. Ejecuta Actions → Construir índice → "
        "Run workflow en GitHub."
    )
    st.stop()

status = report.get("status")
st.subheader("Estado general")
if status == "ok":
    st.success("El catálogo y el índice pasaron todas las verificaciones.")
elif status == "warning":
    st.warning(
        "El índice está disponible, pero el informe contiene advertencias técnicas."
    )
else:
    st.error("El informe detectó inconsistencias que afectan al corpus consultable.")

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

technical_col1, technical_col2 = st.columns(2)
technical_col1.metric(
    "Errores de claves internas", report.get("foreign_key_errors", 0)
)
technical_col2.metric(
    "Desajustes texto–FTS", report.get("fts_rowid_mismatches", 0)
)

st.subheader("Cobertura documental")
st.caption(
    "Comprueba que las publicaciones observadas en la fuente oficial llegan al "
    "catálogo, al manifiesto y finalmente al índice consultable."
)
coverage_layers = corpus_status.get("coverage_layers") or []
if coverage_layers:
    st.dataframe(
        [
            {
                "Etapa": item.get("label"),
                "Disponibles": item.get("numerator", 0),
                "Esperados": item.get("denominator", 0),
                "Cobertura": (
                    f"{item.get('coverage_percent')} %"
                    if item.get("coverage_percent") is not None
                    else "No verificada"
                ),
                "Estado": item.get("status", "unverified"),
            }
            for item in coverage_layers
        ],
        use_container_width=True,
        hide_index=True,
    )
else:
    st.info("La cadena de cobertura todavía no está disponible en el informe.")
source_col1, source_col2, source_col3 = st.columns(3)
source_col1.metric(
    "Fuente oficial verificada",
    "Sí" if corpus_status.get("source_snapshot_valid") else "No",
)
source_col2.metric(
    "Publicaciones observadas",
    corpus_status.get("source_discovered_records", 0),
)
source_age = corpus_status.get("source_age_hours")
source_col3.metric(
    "Antigüedad de la consulta",
    f"{source_age} h" if source_age is not None else "Sin registro",
)

st.markdown("#### Distribución por año")
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
else:
    st.info("No hay información de cobertura anual disponible.")

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
st.markdown("#### Incidencias documentales")
if not any(problems.values()):
    st.success("No se detectaron documentos faltantes ni registros incompletos.")
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

st.subheader("Última actualización del índice")
if indexing_report:
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
else:
    st.info("Aún no existe un informe de actualización del índice.")

st.subheader("Coherencia del índice semántico")
if semantic_state.get("available"):
    semantic_col1, semantic_col2, semantic_col3 = st.columns(3)
    semantic_col1.metric(
        "Capa neuronal",
        "Disponible"
        if semantic_state.get("neural_status") == "ready"
        else "Respaldo local",
    )
    semantic_col2.metric(
        "Fragmentos representados",
        semantic_state.get("documents", 0),
    )
    semantic_col3.metric(
        "Dimensión neuronal",
        semantic_state.get("neural_dimension") or "No aplica",
    )
    st.success(str(semantic_state.get("message") or "Índice semántico vigente."))
else:
    st.warning(
        str(
            semantic_state.get("message")
            or "El índice semántico no está disponible o no corresponde al corpus."
        )
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

st.subheader("Extracción estructurada")
st.caption(
    "Muestra qué campos pudieron identificarse automáticamente para facilitar "
    "la consulta y la comparación de decisiones."
)
quality = report.get("regulatory_quality") or {}
if quality:
    quality_col1, quality_col2, quality_col3, quality_col4, quality_col5 = st.columns(5)
    quality_col1.metric("Fichas", quality.get("total", 0))
    quality_col2.metric("Sin numeral", quality.get("without_numeral", 0))
    quality_col3.metric("Sin fecha de sesión", quality.get("without_session_date", 0))
    quality_col4.metric(
        "Tipo sin clasificar", quality.get("unclassified_request_type", 0)
    )
    quality_col5.metric("Confianza baja", quality.get("low_confidence", 0))
    if quality.get("without_uid"):
        st.warning(
            f"{quality['without_uid']} fichas aún no tienen identificador estable. "
            "Ejecuta Construir índice para actualizar la extracción sobre el corpus."
        )
else:
    st.info(
        "La próxima construcción del índice generará las métricas de extracción."
    )

quality_snapshot = report.get("regulatory_quality_snapshot") or {}
field_rows = quality_snapshot.get("fields") or []
if quality_snapshot.get("status") == "available" and field_rows:
    st.markdown("#### Completitud automática por campo")
    st.caption(
        "Estas cifras indican si el extractor encontró un valor utilizable para "
        "búsqueda, filtros y comparaciones."
    )
    st.dataframe(
        [
            {
                "Campo": item.get("label"),
                "Con valor": item.get("present", 0),
                "Sin extraer": item.get("missing", 0),
                "Cobertura": (
                    f"{float(item['coverage_percent']):.1f} %"
                    if item.get("coverage_percent") is not None
                    else "No disponible en este esquema"
                ),
            }
            for item in field_rows
        ],
        hide_index=True,
        use_container_width=True,
    )
    core_col1, core_col2, core_col3 = st.columns(3)
    core_col1.metric("Fichas medidas", quality_snapshot.get("records", 0))
    core_col2.metric(
        "Fichas con campos núcleo completos",
        quality_snapshot.get("complete_core_records", 0),
    )
    core_percent = quality_snapshot.get("complete_core_percent")
    core_col3.metric(
        "Completitud conjunta",
        f"{float(core_percent):.1f} %" if core_percent is not None else "N/D",
    )

    evidence = quality_snapshot.get("field_evidence") or {}
    if evidence.get("available"):
        st.markdown("#### Procedencia y confianza por campo")
        ev_col1, ev_col2, ev_col3 = st.columns(3)
        ev_col1.metric("Evidencias", evidence.get("rows", 0))
        ev_col2.metric("Fichas con evidencia", evidence.get("records", 0))
        evidence_coverage = evidence.get("record_coverage_percent")
        ev_col3.metric(
            "Fichas trazables",
            f"{float(evidence_coverage):.1f} %"
            if evidence_coverage is not None
            else "N/D",
        )
        evidence_tabs = st.tabs(["Por campo", "Por método"])
        with evidence_tabs[0]:
            st.dataframe(
                [
                    {
                        "Campo": item.get("field"),
                        "Evidencias": item.get("evidence_rows", 0),
                        "Fichas": item.get("records", 0),
                        "Cobertura": (
                            f"{float(item['record_coverage_percent']):.1f} %"
                            if item.get("record_coverage_percent") is not None
                            else "N/D"
                        ),
                        "Confianza media": (
                            f"{float(item['average_confidence']):.1%}"
                            if item.get("average_confidence") is not None
                            else "N/D"
                        ),
                        "Confianza baja": item.get("low_confidence", 0),
                    }
                    for item in evidence.get("by_field") or []
                ],
                hide_index=True,
                use_container_width=True,
            )
        with evidence_tabs[1]:
            st.dataframe(
                [
                    {
                        "Método": item.get("method"),
                        "Origen": item.get("origin", "otro método"),
                        "Evidencias": item.get("evidence_rows", 0),
                        "Confianza media": (
                            f"{float(item['average_confidence']):.1%}"
                            if item.get("average_confidence") is not None
                            else "N/D"
                        ),
                    }
                    for item in evidence.get("by_method") or []
                ],
                hide_index=True,
                use_container_width=True,
            )
    else:
        st.info(
            "La base actual es compatible, pero todavía no contiene evidencia y "
            "confianza por campo. Ejecuta Construir índice con la versión 0.8.0 "
            "para completar esa trazabilidad."
        )

duplicate_hashes = report.get("duplicate_document_hashes") or []
with st.expander(
    f"PDF con contenido duplicado ({len(duplicate_hashes)})",
    expanded=False,
):
    if duplicate_hashes:
        st.write(
            "Dos enlaces pueden apuntar legítimamente al mismo documento o "
            "corresponder a una publicación repetida en la fuente oficial."
        )
        st.dataframe(duplicate_hashes, hide_index=True, use_container_width=True)
    else:
        st.write("No se detectaron hashes de PDF duplicados.")

st.caption(
    f"Generado: {report.get('generated_at', 'sin fecha')} · "
    f"Esquema: {report.get('schema_version', 'desconocido')}"
)
