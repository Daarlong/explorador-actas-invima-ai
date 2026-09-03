from __future__ import annotations

import hashlib

import streamlit as st

from config import (
    CATALOG_REPORT_PATH,
    DATABASE_PATH,
    EVALUATION_CASES_PATH,
    EVALUATION_REPORT_PATH,
    INDEXING_REPORT_PATH,
    INTEGRITY_REPORT_PATH,
    SEMANTIC_INDEX_PATH,
    SEMANTIC_REPORT_PATH,
    SOURCE_SNAPSHOT_PATH,
)
from services.evaluation import (
    MAX_CASES,
    audit_corpus_reports,
    evaluate_cases,
    evaluation_bank_profile,
    evaluation_cases_to_csv,
    evaluation_results_to_csv,
    evaluation_template_csv,
    load_evaluation_report,
    load_evaluation_cases,
    parse_evaluation_cases_csv,
    summarize_evaluation,
)
from services.integrity import load_integrity_report


st.set_page_config(page_title="Evaluación", page_icon="📊", layout="wide")
st.title("📊 Evaluación y calidad")
st.caption(
    "Auditoría del corpus y comparación reproducible de la recuperación "
    "textual, híbrida y semántica"
)


def _metric_percent(value: object) -> str:
    if value is None:
        return "No verificable"
    try:
        return f"{float(value):.1f} %"
    except (TypeError, ValueError):
        return "0.0 %"


audit = audit_corpus_reports(
    INTEGRITY_REPORT_PATH,
    INDEXING_REPORT_PATH,
    SEMANTIC_REPORT_PATH,
    catalog_report_path=CATALOG_REPORT_PATH,
    source_snapshot_path=SOURCE_SNAPSHOT_PATH,
)
st.subheader("1. Auditoría del corpus")
if audit["status"] == "ok":
    st.success("Los reportes no muestran pendientes de cobertura ni integridad.")
elif audit["status"] == "warning":
    st.warning("El corpus está disponible, con observaciones por revisar.")
elif audit["status"] == "error":
    st.error("La auditoría encontró pendientes que pueden afectar los resultados.")
else:
    st.info(
        "Aún no hay un informe válido. Ejecuta Actions → Construir índice en GitHub."
    )

metric1, metric2, metric3, metric4, metric5 = st.columns(5)
metric1.metric(
    "Cobertura documental",
    _metric_percent(audit.get("document_coverage_percent")),
)
metric2.metric(
    "Cobertura de páginas",
    _metric_percent(audit.get("text_page_coverage_percent")),
)
metric3.metric("Documentos faltantes", len(audit.get("missing_documents") or []))
metric4.metric("Páginas candidatas OCR", audit.get("ocr_candidate_pages", 0))
age_hours = audit.get("age_hours")
age_label = "Sin fecha" if age_hours is None else f"{float(age_hours) / 24:.1f} días"
metric5.metric("Antigüedad del informe", age_label)

layer1, layer2, layer3, layer4 = st.columns(4)
layer1.metric(
    "Fuente → catálogo",
    _metric_percent(audit.get("source_catalog_percent")),
)
layer2.metric(
    "Catálogo → manifiesto",
    _metric_percent(audit.get("catalog_manifest_percent")),
)
layer3.metric(
    "Manifiesto → índice",
    _metric_percent(audit.get("document_coverage_percent")),
)
layer4.metric(
    "Páginas consultables",
    _metric_percent(audit.get("text_page_coverage_percent")),
)
st.caption(
    "La cobertura se muestra por capas: una base puede estar completa respecto "
    "del manifiesto aunque la página oficial no haya sido validada correctamente."
)

source_valid = bool(audit.get("source_checked") and audit.get("source_snapshot_valid"))
source_age_hours = audit.get("source_age_hours")
source_age_label = (
    "Sin fecha"
    if source_age_hours is None
    else f"{float(source_age_hours) / 24:.1f} días"
)
with st.expander("Evidencia de la última consulta a la fuente oficial", expanded=False):
    source_col1, source_col2, source_col3 = st.columns(3)
    source_col1.metric("Snapshot válido", "Sí" if source_valid else "No")
    source_col2.metric(
        "Publicaciones detectadas",
        audit.get("source_discovered_records", 0),
    )
    source_col3.metric("Antigüedad de la consulta", source_age_label)
    st.write(
        "Fecha: "
        f"{audit.get('source_snapshot_generated_at') or 'sin registro'} · "
        "Parser: "
        f"{audit.get('source_snapshot_parser_version') or 'sin registro'}"
    )
    snapshot_hash = str(audit.get("source_snapshot_html_sha256") or "")
    if snapshot_hash:
        st.code(f"SHA-256 HTML: {snapshot_hash}", language=None)

coverage = audit.get("coverage_by_year") or []
if coverage:
    with st.expander("Cobertura por año", expanded=False):
        st.dataframe(
            [
                {
                    "Año": item.get("year"),
                    "Esperados": item.get("manifest_documents", 0),
                    "Indexados": item.get("indexed_documents", 0),
                    "Pendientes": item.get("missing_documents", 0),
                    "Cobertura": _metric_percent(item.get("coverage_percent")),
                }
                for item in coverage
            ],
            hide_index=True,
            use_container_width=True,
        )

issues = audit.get("issues") or []
with st.expander(f"Hallazgos de auditoría ({len(issues)})", expanded=bool(issues)):
    if issues:
        labels = {"error": "Error", "warning": "Advertencia", "info": "Info"}
        st.dataframe(
            [
                {
                    "Severidad": labels.get(item.get("severity"), "Info"),
                    "Categoría": item.get("category"),
                    "Hallazgo": item.get("detail"),
                    "Cantidad": item.get("count", 0),
                }
                for item in issues
            ],
            hide_index=True,
            use_container_width=True,
        )
    else:
        st.write("No hay hallazgos pendientes en los reportes disponibles.")

missing_documents = audit.get("missing_documents") or []
if missing_documents:
    with st.expander(
        f"Documentos faltantes ({len(missing_documents)})", expanded=False
    ):
        for title in missing_documents:
            st.write(f"- {title}")

st.divider()
st.subheader("2. Calidad de la extracción regulatoria")
stored_evaluation_report = load_evaluation_report(EVALUATION_REPORT_PATH) or {}
integrity_payload = load_integrity_report(INTEGRITY_REPORT_PATH) or {}
extractor_quality = (
    stored_evaluation_report.get("extractor_quality")
    or integrity_payload.get("regulatory_quality_snapshot")
    or {}
)
quality_fields = extractor_quality.get("fields") or []
if extractor_quality.get("status") == "available" and quality_fields:
    st.caption(
        "La completitud indica si existe un valor automático. No equivale a "
        "precisión; esa medición necesita fichas verificadas por una persona."
    )
    st.dataframe(
        [
            {
                "Campo": item.get("label"),
                "Con valor": item.get("present", 0),
                "Sin extraer": item.get("missing", 0),
                "Cobertura": _metric_percent(item.get("coverage_percent")),
            }
            for item in quality_fields
        ],
        hide_index=True,
        use_container_width=True,
    )
else:
    st.info(
        "Todavía no hay métricas de extracción disponibles. Se generarán al "
        "construir o reprocesar la base."
    )

comparison = stored_evaluation_report.get("extractor_comparison") or {}
if comparison.get("status") == "comparable":
    with st.expander("Comparación antes/después del extractor", expanded=True):
        st.dataframe(
            [
                {
                    "Campo": item.get("label"),
                    "Antes": _metric_percent(item.get("baseline_percent")),
                    "Después": _metric_percent(item.get("current_percent")),
                    "Cambio": (
                        f"{float(item['delta_percentage_points']):+.2f} pp"
                        if item.get("delta_percentage_points") is not None
                        else "No comparable"
                    ),
                }
                for item in comparison.get("fields") or []
            ],
            hide_index=True,
            use_container_width=True,
        )
        st.caption(comparison.get("note") or "")

quality_gate = stored_evaluation_report.get("quality_gate") or {}
if quality_gate:
    gate_status = quality_gate.get("status")
    if gate_status == "pass":
        st.success("El último control de calidad permite publicar esta base.")
    elif gate_status == "fail":
        st.error("El último control de calidad bloqueó la publicación.")
    else:
        st.warning(
            "El último control fue informativo: sus pendientes no bloquearon "
            "la base porque no se ejecutó en modo release."
        )
    with st.expander(
        f"Controles del gate ({quality_gate.get('checks_passed', 0)}/"
        f"{quality_gate.get('checks_total', 0)})",
        expanded=gate_status == "fail",
    ):
        st.dataframe(
            [
                {
                    "Estado": "Cumple" if item.get("passed") else "Pendiente",
                    "Control": item.get("label"),
                    "Detalle": item.get("detail"),
                    "Bloquea": "Sí" if item.get("blocking") else "No",
                }
                for item in quality_gate.get("checks") or []
            ],
            hide_index=True,
            use_container_width=True,
        )

st.divider()
st.subheader("3. Banco de consultas regulatorias")
st.write(
    "Cada caso indica una consulta y una o más actas que un revisor espera "
    "encontrar. El CSV puede guardarse en el repositorio para repetir exactamente "
    "la misma evaluación en versiones futuras."
)

bank_path = EVALUATION_CASES_PATH
stored_cases, stored_errors = load_evaluation_cases(bank_path)
uploaded = st.file_uploader(
    "Usar otro banco CSV en esta sesión",
    type=["csv"],
    help=(
        "El archivo no reemplaza el banco del repositorio. Descárgalo y haz commit "
        "si deseas conservarlo para futuras evaluaciones."
    ),
)
cases = stored_cases
case_errors = list(stored_errors)
bank_source = bank_path.name if bank_path.exists() else "sin banco guardado"
if uploaded is not None:
    raw = uploaded.getvalue()
    if len(raw) > 2 * 1024 * 1024:
        cases = []
        case_errors = ["El banco supera el límite seguro de 2 MiB."]
    else:
        try:
            uploaded_text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            cases = []
            case_errors = ["El CSV debe estar codificado como UTF-8."]
        else:
            cases, case_errors = parse_evaluation_cases_csv(uploaded_text)
            bank_source = uploaded.name

if case_errors:
    st.error("Se omitieron filas inválidas del banco.")
    with st.expander(f"Errores del CSV ({len(case_errors)})", expanded=True):
        for error in case_errors:
            st.write(f"- {error}")

enabled_cases = [case for case in cases if case.enabled]
bank_profile = evaluation_bank_profile(cases)
current_bank_signature = hashlib.sha256(
    evaluation_cases_to_csv(enabled_cases).encode("utf-8")
).hexdigest()
bank_col1, bank_col2, bank_col3, bank_col4 = st.columns(4)
bank_col1.metric("Casos cargados", len(cases))
bank_col2.metric("Casos habilitados", len(enabled_cases))
bank_col3.metric(
    "Decisiones esperadas únicas", bank_profile.get("unique_expected_refs", 0)
)
bank_col4.metric(
    "Caso semaglutida",
    "Sí" if bank_profile.get("has_semaglutida_case") else "Pendiente",
)
st.caption(f"Fuente del banco: {bank_source}")
if (
    len(enabled_cases) < bank_profile.get("minimum_enabled_cases", 15)
    or bank_profile.get("unique_expected_refs", 0)
    < bank_profile.get("minimum_expected_decisions", 30)
    or not bank_profile.get("has_semaglutida_case")
):
    st.warning(
        "El banco todavía sirve para pruebas manuales, pero no cumple el mínimo "
        "del gate de publicación: 15 consultas, 30 decisiones esperadas únicas y "
        "un caso de semaglutida verificado."
    )

if cases:
    st.dataframe(
        [
            {
                "ID": case.case_id,
                "Consulta": case.query,
                "Referencias esperadas": " | ".join(case.expected_refs),
                "K": case.k,
                "Frase exacta": "Sí" if case.exact_phrase else "No",
                "Habilitado": "Sí" if case.enabled else "No",
                "Notas": case.notes,
            }
            for case in cases
        ],
        hide_index=True,
        use_container_width=True,
    )
    st.download_button(
        "Descargar banco actual",
        data=evaluation_cases_to_csv(cases).encode("utf-8-sig"),
        file_name="evaluation-cases.csv",
        mime="text/csv",
    )
else:
    st.info(
        "No hay casos cargados. Descarga la plantilla, completa casos validados "
        "y vuelve a subirla aquí. La aplicación puede seguir usándose normalmente."
    )

st.download_button(
    "Descargar plantilla CSV",
    data=evaluation_template_csv().encode("utf-8-sig"),
    file_name="evaluation-cases-template.csv",
    mime="text/csv",
)
st.caption(
    f"Máximo {MAX_CASES} casos. En `expected_refs` separa fuentes esperadas con `|`: "
    "`acta:2024:01:SEMPB`, `title:Título completo` o `url:https://...`."
)

st.divider()
st.subheader("4. Comparar modos de búsqueda")
mode_labels = {
    "Textual": "textual",
    "Híbrida": "hybrid",
    "Semántica": "semantic",
}
selected_labels = st.multiselect(
    "Modos a evaluar",
    list(mode_labels),
    default=list(mode_labels),
)
selected_modes = [mode_labels[label] for label in selected_labels]
estimated_runs = len(enabled_cases) * len(selected_modes)
st.caption(
    f"Se ejecutarán {estimated_runs} búsquedas. Las métricas se calculan por acta/PDF "
    "único, no por número de fragmentos."
)

run_disabled = not enabled_cases or not selected_modes or not DATABASE_PATH.exists()
if st.button(
    "Ejecutar evaluación",
    type="primary",
    disabled=run_disabled,
    help=(
        "Carga al menos un caso habilitado y selecciona un modo."
        if run_disabled
        else None
    ),
):
    with st.spinner(f"Ejecutando {estimated_runs} búsquedas reproducibles..."):
        results = evaluate_cases(
            DATABASE_PATH,
            SEMANTIC_INDEX_PATH,
            enabled_cases,
            selected_modes,
        )
    st.session_state["evaluation_results"] = results
    st.session_state["evaluation_signature"] = current_bank_signature

results = st.session_state.get("evaluation_results") or []
if results and st.session_state.get("evaluation_signature") != current_bank_signature:
    st.warning(
        "El banco cambió desde la última ejecución. Vuelve a ejecutar la "
        "evaluación para no mezclar resultados anteriores con los casos actuales."
    )
    results = []
if results:
    summary = summarize_evaluation(results)
    st.markdown("#### Resultado agregado")
    st.dataframe(
        [
            {
                "Modo": row["mode"],
                "Casos": row["evaluated"],
                "Errores": row["errors"],
                "Fallback textual": row["fallbacks"],
                "Hit@K": f"{float(row['hit_at_k']):.1%}",
                "Precisión@K": f"{float(row['precision_at_k']):.1%}",
                "Recall@K": f"{float(row['recall_at_k']):.1%}",
                "MRR": f"{float(row['mrr']):.3f}",
                "Tiempo medio": f"{float(row['mean_duration_ms']):.0f} ms",
            }
            for row in summary
        ],
        hide_index=True,
        use_container_width=True,
    )
    st.caption(
        "Hit@K indica si apareció al menos una fuente esperada; Recall@K mide "
        "cuántas fuentes esperadas aparecieron; MRR premia que la primera aparezca "
        "cerca del inicio. Precisión@K penaliza resultados no esperados."
    )

    st.markdown("#### Resultado por consulta")
    st.dataframe(
        [
            {
                "Caso": result.case_id,
                "Modo": result.requested_mode,
                "Modo usado": result.used_mode,
                "Hit@K": f"{result.hit_at_k:.0%}",
                "Precisión@K": f"{result.precision_at_k:.1%}",
                "Recall@K": f"{result.recall_at_k:.1%}",
                "MRR": f"{result.reciprocal_rank:.3f}",
                "Primera posición": result.first_relevant_rank,
                "Encontradas": " | ".join(result.matched_refs),
                "Error": result.error or "",
            }
            for result in results
        ],
        hide_index=True,
        use_container_width=True,
    )
    st.download_button(
        "Exportar resultados CSV",
        data=evaluation_results_to_csv(results).encode("utf-8-sig"),
        file_name="evaluacion-busqueda.csv",
        mime="text/csv",
        type="primary",
    )

    fallback_count = sum(
        result.used_mode != result.requested_mode and not result.error
        for result in results
    )
    if fallback_count:
        st.warning(
            f"{fallback_count} ejecución(es) usaron búsqueda textual como respaldo. "
            "Confirma que el índice semántico esté disponible antes de comparar modos."
        )

if not DATABASE_PATH.exists():
    st.warning("La evaluación de búsquedas se habilitará cuando exista actas.db.")

st.info(
    "Estas métricas miden recuperación contra criterios humanos documentados. "
    "No sustituyen la revisión del concepto y la página original del acta."
)
