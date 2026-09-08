from __future__ import annotations

import html
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
from services.corpus_status import corpus_status_from_reports
from services.database import dashboard_summary
from services.integrity import load_integrity_report
from services.semantic import semantic_index_status
from services.ui_helpers import apply_app_style


st.set_page_config(
    page_title="Explorador de Actas INVIMA",
    page_icon="🔎",
    layout="wide",
)
apply_app_style()


def _install_home_styles() -> None:
    """Aplica una capa visual ligera sin reemplazar controles accesibles."""

    st.markdown(
        """
        <style>
        [data-testid="stMainBlockContainer"] {
            max-width: 1240px;
            padding-top: 2.1rem;
            padding-bottom: 3rem;
        }
        .app-eyebrow {
            color: var(--actas-primary);
            font-size: .78rem;
            font-weight: 750;
            letter-spacing: .11em;
            margin-bottom: .45rem;
            text-transform: uppercase;
        }
        .hero-copy {
            color: color-mix(in srgb, var(--actas-text) 72%, transparent);
            font-size: 1.08rem;
            line-height: 1.6;
            margin: -.35rem 0 1.15rem;
            max-width: 780px;
        }
        .version-pill {
            background: var(--actas-primary-soft);
            border: 1px solid color-mix(in srgb, var(--actas-primary) 28%, transparent);
            border-radius: 999px;
            color: var(--actas-primary);
            display: inline-block;
            font-size: .75rem;
            font-weight: 700;
            margin-bottom: 1.1rem;
            padding: .25rem .62rem;
        }
        div[data-testid="stForm"] {
            background: linear-gradient(
                135deg,
                color-mix(in srgb, var(--actas-primary) 8%, var(--actas-surface)),
                var(--actas-surface)
            );
            border: 1px solid color-mix(in srgb, var(--actas-primary) 22%, transparent);
            border-radius: 1rem;
            padding: 1.05rem 1.1rem .45rem;
        }
        div[data-testid="stForm"] div[data-testid="stTextInputRootElement"] {
            background-color: var(--actas-surface);
        }
        .section-kicker {
            color: color-mix(in srgb, var(--actas-text) 58%, transparent);
            font-size: .78rem;
            font-weight: 750;
            letter-spacing: .08em;
            margin: 1.7rem 0 .2rem;
            text-transform: uppercase;
        }
        div[data-testid="stVerticalBlockBorderWrapper"] {
            border-color: var(--actas-border);
            border-radius: .9rem;
        }
        .flow-title {
            font-size: 1.04rem;
            font-weight: 720;
            line-height: 1.3;
            margin: .1rem 0 .35rem;
        }
        .flow-copy {
            color: color-mix(in srgb, var(--actas-text) 67%, transparent);
            font-size: .9rem;
            line-height: 1.45;
            min-height: 2.65rem;
            margin-bottom: .65rem;
        }
        .corpus-status {
            align-items: center;
            background: var(--secondary-background-color);
            border: 1px solid var(--actas-border);
            border-left: 4px solid #17834b;
            border-radius: .8rem;
            display: flex;
            gap: .85rem;
            justify-content: space-between;
            margin: .45rem 0 .85rem;
            padding: .8rem 1rem;
        }
        .corpus-status.warning { border-left-color: #b7791f; }
        .corpus-status.error { border-left-color: #c33c3c; }
        .status-main {
            align-items: center;
            display: flex;
            gap: .65rem;
        }
        .status-dot {
            background: #17834b;
            border-radius: 50%;
            box-shadow: 0 0 0 4px color-mix(in srgb, #17834b 14%, transparent);
            flex: 0 0 auto;
            height: .62rem;
            width: .62rem;
        }
        .warning .status-dot {
            background: #b7791f;
            box-shadow: 0 0 0 4px color-mix(in srgb, #b7791f 14%, transparent);
        }
        .error .status-dot {
            background: #c33c3c;
            box-shadow: 0 0 0 4px color-mix(in srgb, #c33c3c 14%, transparent);
        }
        .status-title { font-size: .94rem; font-weight: 720; }
        .status-copy {
            color: color-mix(in srgb, var(--actas-text) 63%, transparent);
            font-size: .79rem;
            margin-top: .1rem;
        }
        .status-meta {
            color: color-mix(in srgb, var(--actas-text) 66%, transparent);
            font-size: .79rem;
            text-align: right;
            white-space: nowrap;
        }
        div[data-testid="stMetric"] {
            background: var(--secondary-background-color);
            border-radius: .7rem;
            min-height: 5.5rem;
            padding: .75rem .85rem;
        }
        div[data-testid="stMetricValue"] { font-size: 1.5rem; }
        div[data-testid="stPageLink"] a {
            border-radius: .55rem;
            font-weight: 650;
            justify-content: center;
            min-height: 2.55rem;
            width: 100%;
        }
        .source-note {
            border-top: 1px solid var(--actas-border);
            color: color-mix(in srgb, var(--actas-text) 58%, transparent);
            font-size: .79rem;
            line-height: 1.5;
            margin-top: 1.8rem;
            padding-top: 1rem;
        }
        @media (max-width: 700px) {
            [data-testid="stMainBlockContainer"] { padding-top: 1.2rem; }
            .corpus-status { align-items: flex-start; flex-direction: column; }
            .status-meta { text-align: left; white-space: normal; }
            .flow-copy { min-height: auto; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def _format_count(value: int) -> str:
    return f"{value:,}".replace(",", ".")


def render_home() -> None:
    """Portada orientada a las tres tareas principales de consulta."""

    _install_home_styles()
    ensure_directories()
    summary = dashboard_summary(DATABASE_PATH)
    integrity = load_integrity_report(INTEGRITY_REPORT_PATH) or {}
    catalog = load_catalog(ACTAS_CATALOG_PATH)
    semantic = semantic_index_status(
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
    version_path = Path(__file__).with_name("VERSION")
    version = (
        version_path.read_text(encoding="utf-8").strip()
        if version_path.exists()
        else ""
    )

    range_label = "Sin datos"
    if summary["minimum_year"] and summary["maximum_year"]:
        range_label = f"{summary['minimum_year']}–{summary['maximum_year']}"

    manifest_documents = int(integrity.get("manifest_documents", 0) or 0)
    indexed_documents = int(integrity.get("indexed_documents", 0) or 0)
    coverage = (
        round((indexed_documents / manifest_documents) * 100, 1)
        if manifest_documents
        else 0.0
    )
    pending_documents = len(integrity.get("missing_documents", []) or [])
    ocr_pages = sum(
        len(item.get("pages", []))
        for item in integrity.get("ocr_candidates", []) or []
    )

    if (
        semantic.get("available")
        and semantic.get("neural_status") == "ready"
        and semantic.get("neural_runtime_installed")
    ):
        semantic_label = "Neuronal"
    elif semantic.get("available"):
        semantic_label = "Local"
    else:
        semantic_label = "Pendiente"

    technical_status = integrity.get("status")
    if summary["documents"] == 0:
        status_tone = "error"
        status_title = "Índice no disponible"
        status_copy = "La base documental debe construirse antes de hacer consultas."
    elif technical_status == "error" or corpus_status.get("status") == "error":
        status_tone = "warning"
        status_title = "Corpus disponible con observaciones"
        status_copy = "Puedes consultar; hay verificaciones documentales pendientes."
    elif technical_status == "warning" or corpus_status.get("status") != "ok":
        status_tone = "warning"
        status_title = "Corpus disponible con advertencias"
        status_copy = "La consulta funciona; revisa Integridad para ver el detalle."
    else:
        status_tone = "ok"
        status_title = "Corpus listo para consulta"
        status_copy = "Índice documental y controles técnicos disponibles."

    st.markdown(
        '<div class="app-eyebrow">Sala especializada · INVIMA</div>',
        unsafe_allow_html=True,
    )
    st.title("Encuentra precedentes en las actas")
    st.markdown(
        """
        <div class="hero-copy">
          Busca decisiones por producto, principio activo, interesado, expediente,
          radicado o concepto y verifica el resultado directamente en su fuente.
        </div>
        """,
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<span class="version-pill">Explorador de Actas · v{html.escape(version or "—")}</span>',
        unsafe_allow_html=True,
    )

    with st.form("quick_search"):
        search_col, button_col = st.columns(
            [5.5, 1.25],
            vertical_alignment="bottom",
        )
        quick_query = search_col.text_input(
            "¿Qué necesitas encontrar?",
            placeholder=(
                "Ej.: semaglutida, cambio de indicación, expediente o radicado"
            ),
            help="Puedes escribir una frase, un nombre de producto o un identificador.",
        )
        quick_submit = button_col.form_submit_button(
            "Buscar en actas",
            type="primary",
            use_container_width=True,
        )

    if quick_submit:
        if quick_query.strip():
            st.session_state["explorer_query"] = quick_query.strip()
            st.switch_page("pages/1_Explorador.py")
        else:
            st.warning("Escribe un término, frase o identificador para comenzar.")

    st.markdown(
        '<div class="section-kicker">Empieza por una tarea</div>',
        unsafe_allow_html=True,
    )
    flow_search, flow_compare, flow_analyze = st.columns(3, gap="medium")
    with flow_search:
        with st.container(border=True):
            st.markdown(
                '<div class="flow-title">🔎 Explorar actas</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                '<div class="flow-copy">Filtra resultados, abre la página exacta y conserva la evidencia.</div>',
                unsafe_allow_html=True,
            )
            st.page_link(
                "pages/1_Explorador.py",
                label="Ir al Explorador",
                icon=":material/arrow_forward:",
            )
    with flow_compare:
        with st.container(border=True):
            st.markdown(
                '<div class="flow-title">⚖️ Comparar precedentes</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                '<div class="flow-copy">Contrasta decisiones y construye una cronología regulatoria.</div>',
                unsafe_allow_html=True,
            )
            st.page_link(
                "pages/7_Comparar.py",
                label="Abrir Comparador",
                icon=":material/arrow_forward:",
            )
    with flow_analyze:
        with st.container(border=True):
            st.markdown(
                '<div class="flow-title">✦ Analizar fuentes</div>',
                unsafe_allow_html=True,
            )
            st.markdown(
                '<div class="flow-copy">Reúne fragmentos citables para una consulta documental asistida.</div>',
                unsafe_allow_html=True,
            )
            st.page_link(
                "pages/2_Analista_IA.py",
                label="Abrir Analista",
                icon=":material/arrow_forward:",
            )

    st.markdown(
        '<div class="section-kicker">Estado del corpus</div>',
        unsafe_allow_html=True,
    )
    status_class = (
        "corpus-status"
        if status_tone == "ok"
        else f"corpus-status {status_tone}"
    )
    last_indexed = str(summary.get("last_indexed_at") or "sin registro")
    st.markdown(
        f"""
        <div class="{status_class}">
          <div class="status-main">
            <span class="status-dot"></span>
            <div>
              <div class="status-title">{html.escape(status_title)}</div>
              <div class="status-copy">{html.escape(status_copy)}</div>
            </div>
          </div>
          <div class="status-meta">
            {html.escape(range_label)} · Semántica {html.escape(semantic_label.lower())}<br>
            Última indexación: {html.escape(last_indexed)}
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.expander("Ver cobertura, actividad y detalles técnicos"):
        metric1, metric2, metric3, metric4, metric5 = st.columns(5)
        metric1.metric("Actas", _format_count(summary["unique_acts"]))
        metric2.metric("PDF y partes", _format_count(summary["documents"]))
        metric3.metric("Páginas", _format_count(summary["pages"]))
        metric4.metric(
            "Fichas estructuradas",
            _format_count(summary["regulatory_records"]),
        )
        metric5.metric("Cobertura", f"{coverage} %")

        coverage_col, activity_col = st.columns([1.15, 1], gap="large")
        with coverage_col:
            st.markdown("#### Cobertura por año")
            coverage_rows = integrity.get("coverage_by_year", []) or []
            if coverage_rows:
                chart_data = {
                    "Año": [
                        str(item.get("year"))
                        for item in reversed(coverage_rows)
                    ],
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
            else:
                st.info("El informe de cobertura aún no está disponible.")

        with activity_col:
            st.markdown("#### Documentos recientes")
            latest = summary.get("latest_documents", [])
            if latest:
                st.dataframe(
                    [
                        {
                            "Año": item.get("year"),
                            "Acta": item.get("acta_number"),
                            "Parte": item.get("part") or "Completa",
                            "Documento": item.get("url"),
                        }
                        for item in latest
                    ],
                    hide_index=True,
                    use_container_width=True,
                    column_config={
                        "Documento": st.column_config.LinkColumn(
                            "Fuente",
                            display_text="Abrir",
                        )
                    },
                )
            else:
                st.info("No hay actividad de indexación registrada.")

        st.markdown("#### Indicadores técnicos")
        detail1, detail2, detail3, detail4 = st.columns(4)
        detail1.metric("Catálogo histórico", len(catalog))
        detail2.metric("Pendientes de índice", pending_documents)
        detail3.metric("Candidatas OCR", ocr_pages)
        detail4.metric("Índice semántico", semantic_label)

    st.markdown(
        '<div class="section-kicker">Más recursos</div>',
        unsafe_allow_html=True,
    )
    resource_catalog, resource_integrity, resource_admin = st.columns(3)
    resource_catalog.page_link(
        "pages/5_Catalogo.py",
        label="Consultar catálogo",
        icon="📚",
    )
    resource_integrity.page_link(
        "pages/4_Integridad.py",
        label="Estado e integridad",
        icon=":material/check_circle:",
    )
    resource_admin.page_link(
        "pages/3_Administracion.py",
        label="Administración",
        icon="⚙️",
    )

    st.markdown(
        f"""
        <div class="source-note">
          Las fichas y asociaciones semánticas ayudan a recuperar información,
          pero la fuente de referencia sigue siendo el texto y la página del acta.
          &nbsp;·&nbsp; Informe de integridad:
          {html.escape(str(integrity.get('generated_at', 'sin registro')))}
        </div>
        """,
        unsafe_allow_html=True,
    )


navigation = st.navigation(
    {
        "": [
            st.Page(
                render_home,
                title="Inicio",
                icon=":material/home:",
                default=True,
            ),
            st.Page(
                "pages/1_Explorador.py",
                title="Explorar",
                icon=":material/search:",
            ),
            st.Page(
                "pages/7_Comparar.py",
                title="Comparar",
                icon=":material/compare_arrows:",
            ),
            st.Page(
                "pages/2_Analista_IA.py",
                title="Analizar fuentes",
                icon=":material/auto_awesome:",
            ),
        ],
        "Corpus": [
            st.Page(
                "pages/5_Catalogo.py",
                title="Catálogo",
                icon=":material/library_books:",
            ),
            st.Page(
                "pages/4_Integridad.py",
                title="Integridad",
                icon=":material/verified:",
            ),
        ],
        "Gestión": [
            st.Page(
                "pages/3_Administracion.py",
                title="Administración",
                icon=":material/settings:",
            ),
        ],
    },
    position="top",
)
navigation.run()
