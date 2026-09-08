# Actualización a la versión 0.9.0

La versión 0.9.0 renueva la navegación y la interfaz de la aplicación. Conserva
la extracción, el índice textual y semántico, el visor y las herramientas
documentales de la 0.8.0. No activa ni amplía la integración opcional con IA
generativa y no incorpora un módulo de evaluación o corrección.

La entrega es un **overlay de código**: no incluye ni reemplaza
`documents_manifest.csv`, `actas_catalog.csv` ni ningún archivo de `data/`.

## 1. Identificar el punto de partida

### Si ya instalaste la 0.8.0 y reconstruiste el índice

Para esta actualización solo debes:

1. subir el contenido de la versión 0.9.0;
2. esperar que **Pruebas** termine en verde;
3. esperar el redespliegue automático de Streamlit.

No ejecutes **Construir índice**: la 0.9.0 no modifica el corpus, el esquema ni
las bases de búsqueda.

### Si la aplicación todavía está en la 0.7

La versión 0.9.0 ya contiene los cambios de código de la 0.8.0. No necesitas
instalar primero una entrega 0.8 separada. Debes:

1. subir el contenido de la versión 0.9.0;
2. esperar que **Pruebas** termine en verde;
3. ejecutar **Construir índice** una sola vez;
4. esperar el commit automático y el redespliegue de Streamlit.

Si el repositorio aún conserva las páginas antiguas de evaluación o corrección,
ejecuta una sola vez **Actions → Retirar módulos de evaluación y corrección →
Run workflow**. Las instalaciones que ya pasaron por la 0.8 no necesitan este
paso.

Esa única construcción activa la extracción y el índice semántico de la 0.8.0;
no se hace por el rediseño visual de la 0.9.0.

## 2. Subir la actualización

1. Descomprime el ZIP de la versión 0.9.0.
2. En el repositorio privado abre **Code → Add file → Upload files**.
3. Arrastra **el contenido** de la carpeta descomprimida, no la carpeta
   exterior.
4. Confirma el commit en la rama `main`.

No borres ni reemplaces el contenido actual de `data/`,
`documents_manifest.csv` o `actas_catalog.csv`.

## 3. Verificar el código

Después del commit, el workflow **Pruebas** debe iniciarse automáticamente.
Espera a que quede en verde. Si no se inicia, abre **Actions → Pruebas → Run
workflow** y conserva la rama `main`.

Las pruebas confirman que los flujos documentales existentes siguen operando y
que la navegación, los destinos internos y las utilidades visuales no rompen la
aplicación.

## 4. Construir el índice solo cuando corresponda

Omite por completo esta sección si ya reconstruiste el índice con la 0.8.0.

Si vienes directamente de la 0.7:

1. abre **Actions → Construir índice**;
2. pulsa **Run workflow**;
3. conserva la rama `main` y confirma **Run workflow**;
4. espera que la ejecución y su commit automático terminen en verde.

No necesitas usar **Reprocesar estructura y fichas**, pegar un
`resume_run_id`, descargar artefactos ni subir manualmente las partes de las
bases.

## 5. Comprobar Streamlit

El commit debe activar el redespliegue automático. Espera unos minutos. Si la
aplicación aún muestra la versión anterior, abre su panel de Streamlit y pulsa
**Reboot app** una sola vez.

Comprueba lo siguiente:

1. **Inicio** muestra la versión `0.9.0`, la búsqueda principal, tres accesos
   por tarea y el estado compacto del corpus.
2. La navegación superior muestra **Inicio**, **Explorar**, **Comparar** y
   **Analizar fuentes**; **Catálogo**, **Integridad** y **Administración** están
   agrupados por función.
3. Una búsqueda desde Inicio abre el **Explorador** con la consulta escrita.
4. En **Explorador**, prueba un modo de búsqueda, aplica y restablece filtros,
   cambia de resultado y abre la evidencia en el visor.
5. En el visor, prueba página anterior/siguiente, salto de página, zoom,
   rotación, búsqueda y copia de texto, descarga y enlace al PDF oficial.
6. En **Comparar**, agrega y retira decisiones, revisa diferencias, matriz y
   fuentes, y construye una cronología con filtros y paginación.
7. Abre **Analizar fuentes**, **Catálogo**, **Integridad** y
   **Administración** y confirma que mantienen el mismo sistema visual.
8. Revisa una vez el tema claro y el oscuro, y una ventana estrecha o móvil.
   Los estados deben poder entenderse por su texto, no solo por el color.

## Configuración

Mantén:

```toml
LLM_PROVIDER = "prompt_only"
```

Ese valor evita llamadas generativas y no afecta la búsqueda semántica local.
Si usas la página de Administración, conserva una `ADMIN_PASSWORD` larga y
privada en los secretos de Streamlit; nunca la subas a GitHub.

## Operación posterior

La operación del corpus no cambia. **Actualizar catálogo desde INVIMA** detecta
nuevas publicaciones y dispara **Construir índice** únicamente cuando hay actas
nuevas o documentos pendientes. Las actualizaciones normales continúan siendo
incrementales.

La actualización visual 0.9.0 no exige modificar, descargar ni volver a subir
el catálogo, el manifiesto o las bases existentes.
