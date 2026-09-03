# Explorador de Actas INVIMA + Analista IA

Aplicación privada en Streamlit para explorar actas públicas del INVIMA y
formular preguntas con respuestas sustentadas en documento y página.

Este proyecto conserva las mejores ideas del prototipo
`pdf-search-nn-streamlit` —manifiesto de URLs, descarga de PDFs, extracción por
página y preparación de contexto— y reemplaza el índice JSON por SQLite FTS5,
filtros estructurados y una integración opcional con un proveedor de IA
aprobado.

## Alcance

- Registro automático de las actas publicadas en la página oficial de la Sala
  Especializada de Medicamentos de Síntesis Química y Biológica.
- Catálogo histórico desde 2013 hasta el año en curso, incluyendo las series
  publicadas como SEMPB, SEMNNIMB y SEM y los pronunciamientos conjuntos.
- Índice de búsqueda configurado para todo el histórico visible desde 2013
  hasta el año en curso.
- Corpus heredado inicial desde 2020 hasta 2025 y 2026 hasta el Acta 08: 179
  documentos; el primer workflow añade incrementalmente los años 2013–2019 y
  las series adicionales que encuentre en la página.
- Explorador de actas con búsqueda textual, híbrida, semántica local y frases
  exactas, sin enviar las consultas a servicios externos.
- Filtros por año, número de acta, sala/sección, parte, resultado, producto,
  principio activo, interesado, expediente y radicado.
- Resultados agrupados por acta, paginados, ordenables y con términos
  resaltados de forma segura.
- Visor integrado de la página exacta del PDF, con navegación entre páginas.
- Selección de evidencias en el Explorador para analizarlas directamente en el
  Analista IA.
- Tablero inicial con cobertura real, rango de años, documentos recientes y
  estado de las capacidades.
- Extracción determinística de producto, principio activo, interesado,
  expediente, radicado, solicitud, concepto y resultado normalizado. Estos
  campos se presentan como ayuda pendiente de verificación humana.
- Evidencia con título, página, fragmento y URL de origen claramente identificada.
- Analista IA con citas `[F#]` y negativa cuando no hay evidencia.
- Modo `prompt_only` para trabajar sin enviar consultas a servicios externos.
- Compatibilidad opcional con OpenAI o Azure OpenAI.
- Administración del manifiesto e indexación desde la interfaz o por CLI.
- Índice SQLite compacto, empaquetado en partes verificadas con SHA-256.
- Índice semántico SQLite independiente, local y cuantizado para limitar su
  tamaño; si no está disponible, la aplicación vuelve automáticamente a FTS5.
- Informe de integridad visible desde la aplicación.
- Auditoría por capas: página oficial → catálogo → manifiesto → índice, con
  snapshot y huella SHA-256 del último descubrimiento válido.
- Banco versionable de consultas y métricas Hit@K, Precision@K, Recall@K y MRR
  para comparar objetivamente los tres modos de búsqueda.
- Fichas enriquecidas con numeral, título, fecha de sesión, tipo de solicitud e
  identificador estable independiente del enlace del PDF.
- Cola protegida para corregir, revisar, aprobar o reabrir fichas, conservando
  la huella de la extracción y un historial portable de eventos.
- Comparación lado a lado, cronologías por producto, principio activo,
  expediente o radicado, exportación CSV y reporte imprimible a PDF.
- Actualización incremental para descargar e indexar únicamente actas nuevas.
- Automatización completa: el catálogo lanza el índice cuando detecta cambios o
  documentos pendientes, sin intervención manual.
- Reintentos programados para descargas que fallen temporalmente y uso de URLs
  alternativas verificadas del catálogo cuando el enlace principal no responde.
- OCR de respaldo en español para páginas que no contienen texto extraíble.
- Cobertura del índice desglosada por año y por serie/sala, con informe de los
  errores de la última ejecución.
- Página de Catálogo con estado indexado, pendiente, sin enlace o ya no listado.

No contiene funcionalidades relacionadas con un monitor de transparencia.

## Cambios visibles en la versión 0.7.0

| Módulo | Mejora |
|---|---|
| Fuente | El esquema 6 inventaría cada página física; conserva el texto original o, si falla, el error, además del origen PDF/OCR, calidad y versión del extractor |
| Extracción | Extractor regulatorio v4 para numerales históricos, composición, IFA, DCI, principio activo y combinaciones |
| Evidencia | Cada campo automático muestra valor literal, normalizado y canónico, página, fragmento, método y confianza |
| Correcciones | Un único valor vigente se aplica en filtros, búsqueda, fichas, comparación, cronología y exportaciones |
| Cronología | Combina datos verificados, estructurados, inferidos y menciones textuales sin confundirlos |
| Publicación | Una base candidata se diagnostica y valida; inventario, identidades, seis campos regulatorios, paquetes y banco humano pueden bloquear el reemplazo |

La 0.7.0 no añade funciones de IA. Su objetivo es que el corpus histórico y
las fichas que utilizará una versión posterior sean completos, trazables y
reproducibles.

`LLM_PROVIDER = "prompt_only"` puede mantenerse sin cambios. Ese ajuste solo
controla la generación de respuestas; la búsqueda semántica de esta versión se
ejecuta localmente y no necesita una clave de IA.

### Cobertura heredada antes de la primera actualización

| Año | Sala publicada | PDF/partes |
|---:|---|---:|
| 2020 | SEMNNIMB | 25 |
| 2021 | SEMNNIMB | 39 |
| 2022 | SEMNNIMB | 24 |
| 2023 | SEMNNIMB | 18 |
| 2024 | SEMNNIMB | 27 |
| 2025 | SEMPB | 29 |
| 2026 hasta Acta 08 | SEMPB | 17 |
| **Total** |  | **179** |

La denominación de la sala se conserva tal como fue publicada en cada año.
Una copia histórica de la segunda parte del Acta 01 de 2022 usa un espejo
documental porque esa pieza no aparece enlazada actualmente en la Biblioteca
de INVIMA; el manifiesto la marca como `historical_mirror`.

El catálogo y el índice son componentes distintos. `actas_catalog.csv` conserva
el registro acumulativo de todas las entradas observadas en la página, incluso
si posteriormente se retiran o quedan sin enlace. `documents_manifest.csv`
contiene las entradas con un recurso descargable que pueden incorporarse al
índice de texto. Tanto el catálogo como el índice cubren de forma predeterminada
todo el histórico visible desde 2013; el año inicial se configura con
`ACTAS_INDEX_START_YEAR`.

## Estructura

```text
.
├── home.py
├── pages/
│   ├── 1_Explorador.py
│   ├── 2_Analista_IA.py
│   ├── 3_Administracion.py
│   ├── 4_Integridad.py
│   ├── 5_Catalogo.py
│   ├── 6_Evaluacion.py
│   ├── 7_Comparar.py
│   └── 8_Revision_Fichas.py
├── services/
│   ├── catalog.py
│   ├── database.py
│   ├── downloader.py
│   ├── evaluation.py
│   ├── indexing.py
│   ├── integrity.py
│   ├── llm.py
│   ├── manifest.py
│   ├── metadata.py
│   ├── pdf_reader.py
│   ├── pdf_viewer.py
│   ├── regulatory.py
│   ├── ingredients.py
│   ├── effective_records.py
│   ├── reviews.py
│   ├── comparison.py
│   ├── retrieval.py
│   ├── search.py
│   ├── semantic.py
│   └── text_utils.py
├── tests/
├── actas_catalog.csv
├── documents_manifest.csv
├── build_index.py
├── check_index_pending.py
├── evaluate_search.py
├── reprocess_corpus.py
├── evaluation_cases.csv
├── package_index.py
├── packages.txt
├── sync_catalog.py
└── requirements.txt
```

## Ejecución local cuando Python ya está instalado

No requiere permisos de administrador si el entorno virtual puede crearse en
la carpeta de trabajo:

```powershell
py -m venv .venv
.venv\Scripts\activate
python -m pip install -r requirements.txt
python -m streamlit run home.py
```

Para construir el índice fuera de la interfaz:

```powershell
python build_index.py
```

Si no puedes instalar dependencias localmente, todo el mantenimiento puede
hacerse desde GitHub Actions:

1. Abre **Actions → Actualizar catálogo desde INVIMA → Run workflow**. Este
   flujo consulta la página oficial, conserva las entradas históricas, agrega
   publicaciones nuevas, evita duplicados y, cuando corresponde, inicia
   automáticamente **Construir índice**.
2. Espera a que ambos workflows queden en verde y revisa las páginas
   **Catálogo** e **Integridad** de la aplicación.

El workflow **Construir índice** sigue disponible para una ejecución manual,
pero ya no es necesario lanzarlo cada vez que aparece un acta nueva. Se usa
para actualizaciones incrementales normales. La reconstrucción histórica de la
0.7 se realiza con el workflow separado **Reprocesar estructura y fichas**.

El flujo de construcción del índice:

1. valida el catálogo y ejecuta las pruebas;
2. restaura el índice anterior cuando existe;
3. descarga e indexa únicamente documentos nuevos y prueba los enlaces
   alternativos conocidos si falla el principal;
4. aplica OCR en español a las páginas sin texto y deja registradas las que no
   puedan recuperarse;
5. migra de forma aditiva el esquema anterior y extrae los campos regulatorios
   desde los fragmentos ya almacenados;
6. construye o actualiza el índice semántico local;
7. genera informes de ejecución, integridad y cobertura por año;
8. ejecuta el banco de evaluación y registra sus métricas cuando contiene casos
   habilitados;
9. comprime ambas bases, calcula hashes y las divide en fragmentos de 90 MiB;
10. guarda los índices y los informes automáticamente en el repositorio privado.

No es necesario descargar un artefacto ni subir manualmente `actas.db`.

El workflow de catálogo se ejecuta automáticamente de lunes a viernes a las
18:30, hora de Colombia. Cada consulta válida actualiza la evidencia fechada de
la página oficial; solo dispara la construcción del índice cuando encuentra una
publicación nueva o un documento pendiente. Si el informe de integridad conserva
documentos pendientes por una caída temporal o un enlace problemático, vuelve a
intentar incorporarlos en la siguiente revisión programada.

La primera construcción después de ampliar el índice a 2013 puede tardar varias
horas y aumentar considerablemente el tamaño de la base. El workflow dispone de
un máximo de seis horas, conserva los PDF descargados en caché y deja los
documentos fallidos pendientes para reintentarlos sin perder los correctos.

La primera instalación de la 0.7 migra de forma compatible al esquema 6, pero
el texto fuente histórico solo queda completo después del reprocesamiento
controlado. Abre **Actions → Reprocesar estructura y fichas**: primero ejecuta
el modo `diagnostic` y luego el modo `publish` cuando el banco humano y todos
los controles estén aprobados. Ese flujo trabaja sobre una candidata separada,
permite continuar una ejecución y nunca reemplaza la base publicada durante el
diagnóstico. Después, las actualizaciones normales vuelven a ser incrementales.

Ya no existe una casilla `full_rebuild` en el workflow normal. Esto evita que
una reconstrucción total pueda iniciarse accidentalmente desde la acción
destinada a incorporar actas nuevas.

## Configuración segura de IA

El modo predeterminado es `prompt_only`. En este modo la aplicación busca las
fuentes y genera un contexto para copiar al LLM corporativo aprobado, sin hacer
llamadas externas.

Para habilitar un proveedor, copia el contenido necesario de
`.streamlit/secrets.toml.example` a `.streamlit/secrets.toml`. Este último está
excluido de Git y nunca debe subirse al repositorio.

La página de Administración exige `ADMIN_PASSWORD`. Si no se configura, queda
deshabilitada y el índice solo puede construirse mediante `python build_index.py`.

Valores admitidos para `LLM_PROVIDER`:

- `prompt_only`
- `openai`
- `azure_openai`

La integración debe habilitarse únicamente después de la aprobación de
Seguridad de la Información. Aunque las actas son públicas, las preguntas de
los usuarios podrían contener contexto corporativo.

## Despliegue en Streamlit Community Cloud

1. Crear un repositorio privado vacío en GitHub.
2. Subir el contenido de este proyecto.
3. Crear una aplicación en Streamlit Community Cloud usando `home.py`.
4. Configurar los secretos desde el panel de Streamlit, nunca en GitHub.
5. Ejecutar **Actions → Actualizar catálogo desde INVIMA → Run workflow**.
6. Para una actualización normal, ejecutar una vez **Actions → Construir índice
   → Run workflow**.
7. Para habilitar íntegramente el extractor 0.7 sobre el histórico, seguir
   `INSTRUCCIONES_ACTUALIZACION.md` y usar **Reprocesar estructura y fichas**.
8. Esperar el nuevo despliegue automático de Streamlit.

El sistema de archivos de una aplicación alojada no debe considerarse una base
de datos permanente. Para el piloto, la acción versiona el índice comprimido y
la aplicación lo reconstruye en su directorio temporal al iniciar. Para una
etapa corporativa se recomienda PostgreSQL, Azure Database o un servicio de
búsqueda aprobado.

## Actualización del corpus

La fuente de verdad para descubrir publicaciones es:

<https://www.invima.gov.co/productos-vigilados/medicamentos-y-productos-biologicos/sala-especializada-medicamentos-sintesis>

El recolector incluye únicamente entradas cuyo texto identifica un acta. No
incorpora agendas, cronogramas, resoluciones, lineamientos ni preguntas
frecuentes. Las actas conjuntas publicadas dentro de la misma sección sí se
registran. La actualización es acumulativa: una entrada que desaparezca de la
página queda marcada como histórica/no listada, en lugar de eliminarse.

Para actualizar automáticamente, usa el workflow **Actualizar catálogo desde
INVIMA**. Al terminar, este inicia **Construir índice** únicamente si detectó un
cambio o si quedan documentos pendientes. Desde línea de comandos también se
puede usar `python sync_catalog.py` seguido de `python build_index.py
--allow-partial`.

### Edición manual excepcional

`documents_manifest.csv` acepta como mínimo:

```csv
title,url
Acta No 01 de 2026 SEMPB Primera Parte,https://www.invima.gov.co/...
```

También admite las columnas opcionales `year`, `acta_number`, `section`, `part`
y `source_type`. Si no existen los metadatos documentales, la aplicación
intenta obtenerlos del título.

La edición manual se reserva para corregir un enlace roto o registrar una copia
histórica verificada. Después ejecuta `python sync_catalog.py --bootstrap-only`
para trasladar el cambio al catálogo y luego **Construir índice**. Esa acción
incremental no muestra ni necesita una opción de reconstrucción total.

## Integridad y tamaño del índice

La página **Integridad** muestra:

- documentos esperados e indexados;
- páginas con texto y fragmentos consultables;
- consistencia interna de SQLite y del índice FTS5;
- documentos faltantes o inesperados;
- páginas sin texto que podrían necesitar OCR;
- páginas recuperadas mediante OCR en la última ejecución;
- errores de descarga o procesamiento que se reintentarán;
- enlaces alternativos utilizados;
- cobertura documental por año y por serie/sala;
- avance y errores de la extracción de fichas regulatorias;
- calidad de las fichas: identidad, numeral, fecha, tipo de solicitud y
  confianza;
- claves foráneas, correspondencia entre fragmentos y FTS y posibles PDF
  duplicados por hash;
- estado de construcción del índice semántico local;
- tamaño de la base compactada.

El esquema 6 conserva una copia comprimida del texto fuente por página para
permitir reextracciones auditables, además de los fragmentos necesarios para
buscar. El paquete
`data/actas.db.package.json` registra tamaño y SHA-256 de la base y de cada
fragmento, y la aplicación los valida antes de usar el índice. El índice
semántico se distribuye del mismo modo mediante
`data/semantic.db.package.json`.

La extracción regulatoria es deliberadamente conservadora. Una ficha puede
estar incompleta o mal clasificada por diferencias históricas de formato; por
eso se marca como automática y siempre debe comprobarse contra la página del
PDF mostrada en el visor.

## Evaluación, comparación y revisión humana

La página **Evaluación** no califica respuestas generadas por IA. Mide si el
buscador recupera las actas que un revisor regulatorio definió previamente como
correctas. El archivo `evaluation_cases.csv` comienza con un ejemplo
deshabilitado; completa y habilita casos únicamente después de validar la
fuente esperada. El workflow conserva el resultado en
`data/evaluation-report.json`.

Desde el **Explorador** se pueden marcar evidencias y enviarlas a **Comparar**.
Allí se muestran las fichas lado a lado, se pueden construir cronologías y se
pueden descargar un CSV compatible con Excel y un reporte HTML que el navegador
permite guardar como PDF.

La página **Revisión de fichas** guarda eventos separados del índice automático:
una reconstrucción no sobrescribe las correcciones. Si cambia el PDF o el valor
extraído que sustentaba una revisión, la aplicación exige reconfirmarla; una
coincidencia huérfana o ambigua bloquea la publicación. En el piloto,
el archivo escrito por Streamlit es temporal; tras revisar, descarga
`regulatory-review-log.csv` y haz commit en `data/` para conservar el historial.
Para producción se debe sustituir este mecanismo por una base persistente y SSO.

## Verificación

Las pruebas unitarias no requieren Streamlit:

```powershell
python -m unittest discover -v
```

Antes de publicar una versión se debe comprobar:

- que cada resultado abre el documento correcto;
- que el número de página coincide con el PDF;
- que la IA utiliza únicamente etiquetas `[F#]` disponibles;
- que no se registran preguntas, secretos ni información corporativa;
- que las páginas recuperadas con OCR sean legibles y que las restantes queden
  identificadas como candidatas para revisión.

El flujo `.github/workflows/tests.yml` repite automáticamente la compilación y
las pruebas con Python 3.12 después de cada cambio enviado a GitHub. La versión
0.7.0 usa directamente la API `pymupdf`, sin depender del nombre heredado
`fitz`, y Tesseract se instala dentro del runner de GitHub. La búsqueda
semántica también es local y usa únicamente la biblioteca estándar de Python.
No requiere instalar herramientas de desarrollo en el computador corporativo.

## Propiedad y distribución

Repositorio privado. La titularidad, licencia y política de distribución deben
definirse antes de compartir el código fuera del equipo autorizado.
