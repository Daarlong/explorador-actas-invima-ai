from __future__ import annotations

import hmac
import os

import streamlit as st

from config import (
    ALLOWED_DOCUMENT_HOSTS,
    DATABASE_PATH,
    MANIFEST_PATH,
    PDF_CACHE_DIR,
    ensure_directories,
)
from services.database import database_stats
from services.indexing import rebuild_index
from services.manifest import load_manifest


st.set_page_config(page_title="Administración", page_icon="⚙️", layout="wide")
st.title("⚙️ Administración documental")
st.caption("Carga controlada del manifiesto e indexación de PDFs documentales")

try:
    configured_password = str(st.secrets.get("ADMIN_PASSWORD", "")).strip()
except Exception:
    configured_password = os.getenv("ADMIN_PASSWORD", "").strip()

if not configured_password:
    st.warning(
        "La administración web está deshabilitada. Configura `ADMIN_PASSWORD` "
        "como secreto o ejecuta `python build_index.py` en un entorno autorizado."
    )
    st.stop()

provided_password = st.text_input("Contraseña de administración", type="password")
if not provided_password or not hmac.compare_digest(
    provided_password, configured_password
):
    st.info("Ingresa la contraseña de administración para continuar.")
    st.stop()

ensure_directories()
manifest = load_manifest(MANIFEST_PATH, ALLOWED_DOCUMENT_HOSTS)
stats = database_stats(DATABASE_PATH)

col1, col2, col3 = st.columns(3)
col1.metric("Documentos en manifiesto", len(manifest))
col2.metric("Documentos indexados", stats["documents"])
col3.metric("Fragmentos", stats["chunks"])

with st.expander("Ver manifiesto"):
    st.dataframe([document.as_dict() for document in manifest], use_container_width=True)

st.warning(
    "Reconstruir reemplaza el índice actual. En Streamlit Community Cloud, los "
    "archivos generados durante la ejecución pueden perderse al reiniciar la app; "
    "para un piloto estable conviene generar `data/actas.db` antes del despliegue."
)

if len(manifest) > 50:
    st.info(
        "Este catálogo histórico es demasiado grande para reconstruirlo de forma "
        "confiable desde la sesión web. Ejecuta Actions → Construir índice → "
        "Run workflow en GitHub; el flujo guarda automáticamente el índice "
        "comprimido en el repositorio privado."
    )
    st.stop()

confirmed = st.checkbox("Entiendo y deseo reconstruir el índice")
if st.button("Reconstruir índice", type="primary", disabled=not confirmed):
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
            f"{report.pages_indexed} páginas y {report.chunks_indexed} fragmentos."
        )
    if report.documents_failed:
        st.error(f"Fallaron {report.documents_failed} documentos.")
        for error in report.errors or []:
            st.write(f"- {error}")
    if report.possible_scanned_pages:
        st.warning(
            f"{report.possible_scanned_pages} páginas no contenían texto y podrían "
            "requerir OCR."
        )
