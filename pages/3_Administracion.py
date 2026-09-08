from __future__ import annotations

import hmac
import os

import streamlit as st

from config import (
    ALLOWED_DOCUMENT_HOSTS,
    DATABASE_PATH,
    INTEGRITY_REPORT_PATH,
    MANIFEST_PATH,
    PDF_CACHE_DIR,
    ensure_directories,
)
from services.database import database_stats
from services.indexing import rebuild_index
from services.integrity import build_integrity_report, write_integrity_report
from services.manifest import load_manifest
from services.ui_helpers import apply_app_style


st.set_page_config(page_title="Administración", page_icon="⚙️", layout="wide")
apply_app_style()
st.title("Administración documental")
st.caption(
    "Consulta el estado del corpus y, para catálogos pequeños, ejecuta una "
    "reconstrucción controlada del índice."
)

try:
    configured_password = str(st.secrets.get("ADMIN_PASSWORD", "")).strip()
except Exception:
    configured_password = os.getenv("ADMIN_PASSWORD", "").strip()

if not configured_password:
    st.warning(
        "La administración desde la aplicación está deshabilitada porque no se "
        "configuró `ADMIN_PASSWORD`."
    )
    st.caption(
        "El índice puede seguir administrándose mediante GitHub Actions o desde "
        "un entorno autorizado."
    )
    st.stop()

with st.container(border=True):
    st.subheader("Acceso restringido")
    st.caption(
        "Esta sección puede iniciar procesos que modifican el índice de búsqueda."
    )
    provided_password = st.text_input(
        "Contraseña de administración",
        type="password",
        placeholder="Ingresa la contraseña configurada en los secretos",
    )
    if not provided_password or not hmac.compare_digest(
        provided_password, configured_password
    ):
        st.info("Ingresa la contraseña de administración para continuar.")
        st.stop()

ensure_directories()
manifest = load_manifest(MANIFEST_PATH, ALLOWED_DOCUMENT_HOSTS)
stats = database_stats(DATABASE_PATH)

status_tab, rebuild_tab = st.tabs(["Estado actual", "Reconstrucción excepcional"])

with status_tab:
    st.subheader("Resumen del índice")
    col1, col2, col3 = st.columns(3)
    col1.metric("Documentos en manifiesto", len(manifest))
    col2.metric("Documentos indexados", stats["documents"])
    col3.metric("Fragmentos consultables", stats["chunks"])

    with st.expander(f"Consultar manifiesto ({len(manifest)} documentos)"):
        if manifest:
            st.dataframe(
                [document.as_dict() for document in manifest],
                use_container_width=True,
                hide_index=True,
            )
        else:
            st.info("El manifiesto no contiene documentos.")

with rebuild_tab:
    st.subheader("Reconstruir desde la sesión web")
    st.warning(
        "Esta operación reemplaza el índice actual. Los archivos generados en "
        "Streamlit Community Cloud pueden perderse cuando la aplicación se reinicia."
    )

    if len(manifest) > 50:
        with st.container(border=True):
            st.markdown("**Para este corpus usa GitHub Actions**")
            st.write(
                "El catálogo histórico es demasiado grande para procesarlo de forma "
                "confiable dentro de una sesión web."
            )
            st.code(
                "Actions → Construir índice → Run workflow",
                language="text",
            )
            st.caption(
                "El workflow construye, comprime y guarda el índice en el "
                "repositorio privado."
            )
    else:
        confirmed = st.checkbox(
            "Entiendo que la reconstrucción reemplazará el índice de esta sesión"
        )
        if st.button(
            "Reconstruir índice",
            type="primary",
            disabled=not confirmed,
            use_container_width=True,
        ):
            progress = st.progress(0.0)
            status = st.empty()

            def update_progress(current: int, total: int, title: str) -> None:
                progress.progress(current / total)
                status.write(f"Procesando {current}/{total}: {title}")

            with st.spinner("Descargando, leyendo e indexando actas..."):
                report = rebuild_index(
                    MANIFEST_PATH,
                    DATABASE_PATH,
                    PDF_CACHE_DIR,
                    progress_callback=update_progress,
                )

            status.write("Proceso terminado")
            if report.documents_indexed:
                st.success(
                    f"Se indexaron {report.documents_indexed} documentos, "
                    f"{report.pages_indexed} páginas y "
                    f"{report.chunks_indexed} fragmentos."
                )
                integrity = build_integrity_report(
                    DATABASE_PATH,
                    MANIFEST_PATH,
                    ALLOWED_DOCUMENT_HOSTS,
                )
                write_integrity_report(integrity, INTEGRITY_REPORT_PATH)
            if report.documents_failed:
                st.error(f"Fallaron {report.documents_failed} documentos.")
                for error in report.errors or []:
                    st.write(f"- {error}")
            if report.possible_scanned_pages:
                st.warning(
                    f"{report.possible_scanned_pages} páginas no contenían texto "
                    "y podrían requerir OCR."
                )
