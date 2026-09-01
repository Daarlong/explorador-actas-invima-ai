# Actualización a la versión 0.4.0

Este paquete está preparado para actualizar un repositorio que ya contiene el
catálogo y la base construida. Por seguridad, no incluye los archivos mutables:

- `actas_catalog.csv`
- `documents_manifest.csv`
- `data/actas.db.gz.part-*`
- `data/actas.db.package.json`
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
7. Ejecuta una vez **Actions → Actualizar catálogo desde INVIMA → Run workflow**.

Si el catálogo tiene novedades o el informe conserva documentos pendientes,
ese workflow iniciará automáticamente **Construir índice**. Déjalo sin
`full_rebuild`: la ejecución normal reutiliza la base y descarga solo lo
pendiente.

## Comprobación

Cuando ambos workflows terminen:

1. Espera el redespliegue de Streamlit.
2. Abre **Catálogo** y verifica que los años comiencen en 2013.
3. Abre **Integridad** y revisa la tabla de cobertura por año.
4. Si aparece algún documento fallido, no borres la base: quedará pendiente y
   será reintentado automáticamente en la siguiente revisión.
