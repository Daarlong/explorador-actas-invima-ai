# Actualización correctiva a la versión 0.7.1

Esta actualización incorpora el extractor regulatorio v5 y el reprocesamiento
seguro del histórico. El ZIP no contiene ni reemplaza los archivos mutables:

- `actas_catalog.csv`;
- `documents_manifest.csv`;
- `evaluation_cases.csv`;
- las partes de `actas.db` y `semantic.db`;
- los informes actuales de `data/`;
- `data/regulatory-review-log.csv`.

La aplicación puede abrir la base 0.6 después de subir el código. Sin embargo,
las mejoras masivas de extracción solo quedarán activas en todo el histórico
cuando termine el flujo de diagnóstico y publicación descrito abajo.

## 1. Subir el código

1. Descomprime el ZIP.
2. En el repositorio privado abre **Code → Add file → Upload files**.
3. Abre la carpeta descomprimida y arrastra **su contenido**, no la carpeta
   exterior.
4. Confirma que GitHub muestra `.github/workflows`, `services`, `pages`,
   `tests`, `reprocess_corpus.py` y `ALCANCE_V0.7.md`.
5. Crea el commit en `main`.
6. Espera a que **Actions → Pruebas** termine en verde.

No ejecutes una reconstrucción total desde **Construir índice**. En la 0.7 no
hay casilla `full_rebuild`: el reprocesamiento histórico tiene su propio flujo
y trabaja sobre una base candidata aislada.

## 2. Construir la candidata por lotes

1. Abre **Actions → Reprocesar estructura y fichas**.
2. Pulsa **Run workflow**.
3. Elige:
   - `mode`: `diagnostic`;
   - `resume_run_id`: vacío en la primera ejecución;
   - `max_batches`: deja `4` como opción segura.
4. Pulsa el botón verde **Run workflow** y espera a que termine.

Si acabas de instalar una corrección que modifica el extractor, el esquema o
las dependencias, deja `resume_run_id` vacío aunque tengas un diagnóstico
anterior completo. Cada checkpoint queda vinculado al código, al entorno de
extracción y a la base publicada con los que fue creado. GitHub reutiliza la
caché de PDF, de modo que la reconstrucción necesaria vuelve a procesar los
documentos pero normalmente no los descarga otra vez.

Cada lote procesa hasta 50 documentos. Con el valor predeterminado, una
ejecución procesa como máximo 200 y guarda un punto de continuación. Esto evita
perder varias horas de trabajo si GitHub interrumpe el runner.

Si aparece el aviso **Reprocesamiento parcial**, repite el flujo así:

1. copia el número de la ejecución que acaba de terminar; es el número visible
   en el título y también aparece en la URL después de `/runs/`;
2. vuelve a pulsar **Run workflow**;
3. conserva `mode = diagnostic` y `max_batches = 4`;
4. pega ese número en `resume_run_id`.

En cada continuación se debe usar el número de la ejecución inmediatamente
anterior. Repite hasta que se ejecuten en verde los pasos **Finalizar fichas**,
**Reconciliar identidades**, **Empaquetar candidata** y **Restaurar paquetes**.
También puedes usar `max_batches = 0` para intentar procesar todo en una sola
ejecución, pero el valor `4` es más resistente a límites de tiempo.

El modo `diagnostic`:

- no modifica la base publicada;
- conserva el texto fuente por página y aplica el extractor v5;
- compara cobertura antes y después;
- comprueba identidades y revisiones humanas;
- restaura los paquetes y realiza una búsqueda de prueba;
- deja los artefactos `reprocess-report-...` y `reprocess-checkpoint-...` en el
  resumen de la ejecución.

Una ejecución diagnóstica puede aparecer en verde aunque haya encontrado una
candidata no publicable: verde significa que logró completar el diagnóstico.
Lee siempre el recuadro **Resultado del reprocesamiento** en **Summary**. Si el
estado es `rejected`, GitHub muestra advertencias y la causa exacta.

## 3. Completar el banco humano de publicación

El diagnóstico puede terminar correctamente con un banco incompleto. La
publicación, en cambio, exige criterios humanos reales para evitar aprobar el
extractor solo porque llena más campos.

En la página **Evaluación** de Streamlit:

1. descarga la plantilla CSV;
2. documenta al menos 15 consultas habilitadas y 30 referencias oficiales
   esperadas distintas;
3. incluye y verifica el caso `Semaglutida` de `acta:2017:14:SEMPB`;
4. cambia `enabled` a `true` únicamente después de comprobar cada referencia;
5. guarda el archivo como `evaluation_cases.csv`;
6. súbelo a la raíz del repositorio y crea el commit.

Las referencias admiten `acta:AÑO:NÚMERO:SALA`, `title:TÍTULO` o
`url:ENLACE`. Se separan con `|` cuando una consulta tiene varias respuestas
esperadas. No inventes referencias para alcanzar el mínimo: cada fila debe
provenir de una comprobación humana del acta oficial.

## 4. Publicar la candidata aprobada

1. Abre otra vez **Actions → Reprocesar estructura y fichas → Run workflow**.
2. Selecciona `mode = publish`.
3. En `resume_run_id`, pega el número de la **última ejecución diagnóstica
   completa**.
4. Deja `max_batches = 4`; como la candidata ya está completa, no repetirá los
   lotes.
5. Ejecuta el workflow.

La publicación se bloquea automáticamente si encuentra una regresión de
cobertura, un paquete dañado, un banco inválido, una revisión huérfana o una
identidad ambigua. Si todo pasa, GitHub hace un commit automático con las
nuevas partes de las bases y los informes. El CSV de revisiones se comprueba
por hash y nunca se sustituye como parte de esa publicación.

Si el flujo falla, abre el artefacto `reprocess-report-...` y revisa:

- `reprocess-report.json`: resultado consolidado y autoritativo;
- `candidate-evaluation-report.json`: evaluación de la candidata;
- `baseline-evaluation-report.json`: evaluación de la base publicada anterior.

Los nombres distintos evitan confundir la candidata con la referencia. Puedes
corregir el banco o la revisión indicada y continuar usando el número de esa
ejecución mientras el checkpoint siga disponible y no hayan cambiado el
código de extracción ni la base publicada. Los checkpoints se conservan
durante 7 días y, cuando ya existe, incluyen también el índice semántico para
evitar reconstruirlo innecesariamente durante la publicación.

## 5. Comprobar Streamlit

1. Espera el redespliegue automático. Si después de unos minutos aún muestra la
   base anterior, abre el panel de Streamlit y pulsa **Reboot app**.
2. En **Inicio**, confirma `versión 0.7.1`.
3. En **Integridad**, confirma esquema `6`, texto fuente disponible y cero
   documentos pendientes.
4. En **Explorador**, busca `Semaglutida`, abre la ficha de Ozempic y comprueba
   la evidencia del principio activo en el PDF.
5. Activa **Solo fichas sin principio activo** para revisar pendientes reales.
6. En **Comparar**, construye una cronología y comprueba las etiquetas:
   verificada, revisada, estructurada, inferida o mención textual.
7. En **Revisión de fichas**, confirma que los vacíos aparecen como **No
   extraído** y que una corrección se refleja también en filtros y exportación.

Mantén `LLM_PROVIDER = "prompt_only"`. La versión 0.7 no añade ni necesita
nuevas llamadas a IA.

## Operación posterior

Después de publicar la 0.7, el mantenimiento normal vuelve a ser automático:
**Actualizar catálogo desde INVIMA** detecta publicaciones y dispara
**Construir índice** solo cuando hay actas nuevas o pendientes. No es necesario
repetir el reprocesamiento histórico para cada acta nueva.
