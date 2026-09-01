# Actualización a la versión 0.5.0

Este paquete está preparado para actualizar un repositorio que ya contiene el
catálogo y la base construida. Por seguridad, no incluye los archivos mutables:

- `actas_catalog.csv`
- `documents_manifest.csv`
- `data/actas.db.gz.part-*`
- `data/actas.db.package.json`
- `data/semantic.db.gz.part-*`
- `data/semantic.db.package.json`
- los informes actuales dentro de `data/`

De esta forma, cargar la actualización no reemplaza la cobertura que ya obtuvo
el workflow ni obliga a reconstruir toda la base.

## Instalación desde el navegador

1. Descomprime el ZIP.
2. En el repositorio privado abre **Code → Add file → Upload files**.
3. Abre la carpeta descomprimida y arrastra **su contenido**, no la carpeta
   exterior.
4. Confirma que GitHub muestra también `.github/workflows`, `services`, `pages`
   y `tests`.
5. Crea el commit en `main`.
6. Espera a que **Actions → Pruebas** termine en verde.
7. Ejecuta una vez **Actions → Construir índice → Run workflow**.

Deja desmarcada la opción **Reconstruir todos los PDF** (`full_rebuild`). Si
GitHub no muestra la casilla, el valor predeterminado ya es `false`: solo pulsa
**Run workflow**. Esta ejecución reutiliza la base que ya construiste.

La primera ejecución con la versión 0.5.0 puede tardar más que una actualización
normal porque realiza tres operaciones nuevas:

1. migra los esquemas anteriores al esquema 4 sobre una copia y verifica que no
   cambien los documentos, páginas ni fragmentos;
2. extrae las fichas regulatorias desde el texto ya indexado;
3. construye y empaqueta `semantic.db`.

No vuelve a descargar todos los PDF y no requiere `full_rebuild`. Al finalizar,
la acción hará un commit automático con los paquetes de los índices y los
informes. Espera a que la ejecución completa quede en verde.

## Comprobación

1. Espera el redespliegue de Streamlit o reinicia la aplicación desde su panel.
2. En **Inicio**, confirma que aparece `versión 0.5.0` y que la búsqueda
   semántica figura como disponible.
3. En **Explorador**, comprueba los modos Híbrida, Textual y Semántica local,
   los resultados agrupados y el botón **Ver página**.
4. Selecciona un fragmento y pulsa **Analizar seleccionadas** para comprobar la
   integración con el Analista IA.
5. Abre **Integridad** y revisa los registros regulatorios, el estado semántico
   y la cobertura por año.
6. Si aparece algún documento fallido, no borres la base: quedará pendiente y
   será reintentado automáticamente en la siguiente revisión.

Mantén `LLM_PROVIDER = "prompt_only"` si todavía no existe una API corporativa
aprobada. La búsqueda semántica local funciona igualmente en ese modo.
