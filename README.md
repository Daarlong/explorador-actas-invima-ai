# Explorador de Actas INVIMA

Aplicación privada en Streamlit para consultar, comparar y analizar las actas
públicas del INVIMA con trazabilidad al documento y a la página de origen.

Este proyecto conserva las mejores ideas del prototipo
`pdf-search-nn-streamlit` —manifiesto de URLs, descarga de PDFs, extracción por
página y preparación de contexto— y reemplaza el índice JSON por SQLite FTS5,
filtros estructurados y búsqueda semántica local.

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
- Visor integrado del PDF con apertura en la página exacta, salto de página,
  zoom, rotación, búsqueda y copia de texto.
- Selección de evidencias en el Explorador para compararlas directamente.
- Tablero inicial con cobertura real, rango de años, documentos recientes y
  estado de las capacidades.
- Extracción determinística de producto, principio activo, interesado,
  expediente, radicado, solicitud, concepto y resultado normalizado. Estos
  campos facilitan la consulta y siempre pueden contrastarse con la evidencia
  del PDF oficial.
- Evidencia con título, página, fragmento y URL de origen claramente identificada.
- Modo `prompt_only`, sin consulta generativa ni envío de preguntas a servicios
  externos.
- Administración del manifiesto e indexación desde la interfaz o por CLI.
- Índice SQLite compacto, empaquetado en partes verificadas con SHA-256.
- Índice semántico SQLite independiente con representaciones neuronales
  multilingües locales y cuantizadas; si ese complemento no está disponible,
  la aplicación conserva la búsqueda semántica determinística y FTS5.
- Construcción neuronal reanudable por segmentos, con checkpoint confirmado en
  GitHub Actions y continuación automática hasta alcanzar la cobertura total.
- Reutilización de embeddings por SHA-256 del texto exacto: una actualización
  posterior calcula únicamente los fragmentos nuevos o modificados.
- Informe de integridad visible desde la aplicación.
- Auditoría por capas: página oficial → catálogo → manifiesto → índice, con
  snapshot y huella SHA-256 del último descubrimiento válido.
- Fichas enriquecidas con numeral, título, fecha de sesión, tipo de solicitud e
  identificador estable independiente del enlace del PDF.
- Búsqueda directa y paginada de fichas dentro de **Comparar**, diferencias
  resaltadas y acceso al visor desde cada evidencia.
- Cronologías paginadas por producto, principio activo, expediente, radicado,
  interesado, resultado o tipo de solicitud, con filtros, total de resultados,
  exportación CSV y reporte imprimible.
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

## Corrección incorporada en la versión 0.9.2

La 0.9.2 corrige la búsqueda de frases sin modificar las bases ya publicadas.
La opción **Exigir frase completa** ahora es compatible con el índice FTS5
compacto (`detail=column`): preselecciona por términos individuales y confirma
la secuencia literal antes de aplicar los límites de resultados.

Las búsquedas textual e híbrida también incorporan un carril literal. Si la
consulta aparece palabra por palabra en un fragmento, esa evidencia no se
pierde detrás de coincidencias que solo contienen los términos dispersos; en
modo híbrido se conserva y prioriza frente a las paráfrasis neuronales. La
comparación ignora mayúsculas, tildes, puntuación y saltos de línea, y las
consultas con guiones, barras o guion bajo ya no generan frases FTS inválidas.

Esta entrega es un overlay de código. No incluye ni reemplaza `data/`,
`documents_manifest.csv` o `actas_catalog.csv`, y **no requiere ejecutar
Construir índice ni recalcular embeddings**. Basta con subir el contenido del
ZIP, esperar que **Pruebas** termine en verde y dejar que Streamlit se
redespliegue. El alcance cerrado está en `ALCANCE_V0.9.2.md`.

## Corrección incorporada en la versión 0.9.1

La 0.9.1 corrige la primera construcción del índice neuronal para corpus que no
alcanzan a completarse dentro de una sola ejecución de GitHub Actions. El
usuario inicia **Construir índice** una sola vez. Cada ejecución procesa como
máximo 7.200 segundos o 50.000 textos únicos, confirma el avance y programa el
siguiente segmento automáticamente. El límite de seguridad de la cadena es de
20 segmentos.

| Área | Comportamiento en 0.9.1 |
|---|---|
| Continuación | El siguiente segmento se inicia automáticamente; no se utiliza `resume_run_id` ni se descargan artefactos manualmente |
| Checkpoint | `data/semantic.checkpoint.db` conserva el trabajo parcial en Actions Cache y confirma cada lote de 128 textos |
| Progreso | `data/semantic-progress.json` registra los vectores disponibles, los pendientes y los segmentos terminados |
| Publicación segura | Las bases publicadas permanecen intactas durante todos los segmentos; `semantic.db` solo se reemplaza y empaqueta cuando llega al 100 % |
| Reutilización | Los vectores se identifican por SHA-256 del texto exacto, incluso si cambian los identificadores internos de SQLite |
| Actualizaciones futuras | Una nueva acta reutiliza los embeddings vigentes y calcula únicamente textos nuevos o modificados |

La segmentación evita perder horas de cálculo, pero no elimina el trabajo de la
primera carga. Un histórico grande puede necesitar varias ejecuciones y consumir
una cantidad importante de minutos de GitHub Actions. Es normal que aparezcan
varias ejecuciones consecutivas de **Construir índice**: cada una puede terminar
en verde mientras la siguiente queda en cola.

Para reconocer el avance, abre el **Summary** de la ejecución más reciente y
busca **Avance del índice neuronal**. Allí se muestran el estado, los embeddings
disponibles, los pendientes y los segmentos terminados. En los registros también
aparece `Embeddings neuronales confirmados: X/Y`. La cadena terminó solamente
cuando el estado sea **completo**, los pendientes sean `0` y el workflow haya
creado el commit automático que despliega el índice final en Streamlit.

El alcance cerrado de esta corrección está en `ALCANCE_V0.9.1.md`.

## Cambios visibles en la versión 0.9.0

La 0.9.0 renueva la experiencia de uso sin cambiar el corpus, los manifiestos,
las bases publicadas ni el funcionamiento de búsqueda incorporado en la 0.8.0.

| Área | Mejora |
|---|---|
| Navegación | Una barra superior reúne **Inicio**, **Explorar**, **Comparar** y **Analizar fuentes**; **Catálogo**, **Integridad** y **Administración** quedan organizados por función |
| Sistema visual | Componentes, tipografía, espaciado, estados y controles comparten un estilo consistente que funciona con los temas claro y oscuro de Streamlit |
| Portada | La búsqueda es la acción principal; tres accesos orientados por tarea llevan a explorar, comparar o preparar un análisis y los indicadores técnicos quedan en un detalle desplegable |
| Explorador | Los resultados usan una vista lista–detalle, filtros agrupados y restablecibles, estados vacíos claros, selección de evidencias y acceso inmediato a la página de origen |
| Visor | La evidencia permanece junto a los resultados y conserva salto de página, navegación, zoom, rotación, búsqueda, copia de texto, descarga y apertura del PDF oficial |
| Comparar y cronología | La selección, las diferencias, la matriz, las fuentes y la cronología tienen una jerarquía más clara; se mantienen filtros, paginación y descargas |
| Páginas secundarias | Analizar fuentes, Catálogo, Integridad y Administración adoptan los mismos títulos, ayudas, contenedores y estados visuales |
| Enfoque documental | Se retiran de la interfaz las referencias a flujos de evaluación o corrección; las actas continúan siendo el insumo de consulta y análisis |

La interfaz sigue construida con controles nativos de Streamlit. El estilo
compartido vive en `services/ui_helpers.py`, mientras que los temas claro y
oscuro se definen en `.streamlit/config.toml`. Esta actualización no añade un
frontend separado, componentes JavaScript ni dependencias de imágenes o
fuentes externas.

Si el índice ya fue reconstruido con la versión 0.8.0, instalar la 0.9.0 solo
requiere subir el código, esperar **Pruebas** y dejar que Streamlit se
redespliegue. No se debe ejecutar **Construir índice** por este cambio visual.
Si la aplicación sigue en la 0.7, la entrega 0.9.0 sustituye también el código
de la 0.8.0 y sí exige ejecutar **Construir índice** una vez para activar sus
mejoras de extracción y semántica.

El alcance cerrado de esta entrega está en `ALCANCE_V0.9.md`. Los documentos
`ALCANCE_V0.7.md` y `ALCANCE_V0.8.md` se conservan como registro histórico.

## Cambios incorporados en la versión 0.8.0

| Módulo | Mejora |
|---|---|
| Extracción masiva | El extractor reconoce más disposiciones históricas, rótulos regulatorios, productos, composiciones, principios activos, solicitudes y conceptos a partir del texto fuente ya almacenado |
| Semántica neuronal | Se añade un modelo multilingüe local para mejorar el orden de candidatos; el buscador conserva una ruta de respaldo cuando el modelo no puede cargarse |
| Comparación | Las fichas se buscan y agregan desde la propia página, con paginación y diferencias visibles entre campos |
| Cronología | Incorpora nuevos ejes y filtros, informa el total y pagina conjuntos extensos sin confundir una mención textual con un campo estructurado |
| Visor | Permite saltar de página, ampliar, rotar, buscar y copiar texto, y abrir la evidencia desde Explorador, el asistente de fuentes o Comparar |
| Seguridad documental | La caché comprueba la identidad y la huella del PDF antes de reutilizarlo para evitar asociaciones incorrectas |
| Enfoque | No hay módulos de evaluación o corrección manual; `prompt_only` mantiene inactiva por defecto la compatibilidad opcional con proveedores generativos |

La ampliación del extractor no implica una métrica de exactitud todavía no
medida. Los campos derivados siguen siendo ayudas de navegación y deben
contrastarse con el texto y la página del PDF oficial.

`LLM_PROVIDER = "prompt_only"` puede mantenerse sin cambios. Ese ajuste solo
evita llamadas generativas; la búsqueda semántica neuronal de esta versión se
ejecuta localmente y no necesita una clave de API.

El alcance histórico de la 0.7.2 se conserva en `ALCANCE_V0.7.md` y el alcance
cerrado de la base técnica 0.8.0 está en `ALCANCE_V0.8.md`.

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
├── .streamlit/
│   └── config.toml
├── home.py
├── pages/
│   ├── 1_Explorador.py
│   ├── 2_Analista_IA.py
│   ├── 3_Administracion.py
│   ├── 4_Integridad.py
│   ├── 5_Catalogo.py
│   └── 7_Comparar.py
├── services/
│   ├── catalog.py
│   ├── corpus_status.py
│   ├── database.py
│   ├── downloader.py
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
│   ├── text_utils.py
│   └── ui_helpers.py
├── tests/
├── actas_catalog.csv
├── documents_manifest.csv
├── build_index.py
├── check_index_pending.py
├── reprocess_corpus.py
├── package_index.py
├── sync_catalog.py
├── ALCANCE_V0.7.md
├── ALCANCE_V0.8.md
├── ALCANCE_V0.9.md
├── ALCANCE_V0.9.1.md
├── ALCANCE_V0.9.2.md
├── INSTRUCCIONES_ACTUALIZACION.md
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
para actualizaciones incrementales normales y para activar una nueva versión
del extractor sobre el texto fuente ya almacenado. Desde la 0.9.1, una sola
ejecución manual basta también para la primera carga neuronal: si no termina en
el primer segmento, el propio workflow programa los siguientes.

El flujo de construcción del índice:

1. valida el catálogo y ejecuta las pruebas;
2. restaura el índice anterior cuando existe;
3. descarga e indexa únicamente documentos nuevos y prueba los enlaces
   alternativos conocidos si falla el principal;
4. aplica OCR en español a las páginas sin texto y deja registradas las que no
   puedan recuperarse;
5. migra de forma aditiva el esquema anterior y extrae los campos regulatorios
   desde los fragmentos ya almacenados;
6. construye o actualiza el índice semántico local y avanza el complemento
   neuronal multilingüe en un segmento de hasta 7.200 segundos o 50.000 textos
   únicos;
7. genera informes de ejecución, integridad y cobertura por año;
8. si quedan embeddings, guarda `semantic.checkpoint.db` y
   `semantic-progress.json` en Actions Cache y programa el siguiente segmento;
9. al alcanzar el 100 %, comprime ambas bases, calcula hashes, las divide en
   fragmentos de 90 MiB y guarda los índices y los informes automáticamente en
   el repositorio privado.

No es necesario volver a pulsar **Run workflow**, descargar un artefacto, pegar
un identificador de ejecución ni subir manualmente `actas.db`.

El workflow de catálogo se ejecuta automáticamente de lunes a viernes a las
18:30, hora de Colombia. Cada consulta válida actualiza la evidencia fechada de
la página oficial; solo dispara la construcción del índice cuando encuentra una
publicación nueva o un documento pendiente. Si el informe de integridad conserva
documentos pendientes por una caída temporal o un enlace problemático, vuelve a
intentar incorporarlos en la siguiente revisión programada.

La primera construcción después de ampliar el índice a 2013 puede tardar muchas
horas acumuladas y aumentar considerablemente el tamaño de la base. Cada
segmento termina voluntariamente antes del máximo del runner, conserva su avance
en Actions Cache y deja otro segmento en cola. Esta primera carga puede consumir
una cantidad importante de minutos de GitHub Actions; dividirla evita perder el
cálculo, pero no reduce el cálculo total requerido por el modelo.

Durante esa cadena, cada segmento verde confirma que su avance quedó guardado;
no significa necesariamente que el índice completo ya esté publicado. Abre el
**Summary** de la ejecución más reciente y revisa **Avance del índice neuronal**.
La finalización se reconoce por `Estado: completo`, `Pendientes: 0` y el commit
automático de las bases empaquetadas. Hasta entonces, Streamlit continúa usando
sin modificaciones el índice que ya estaba publicado.

Al instalar la 0.8 sobre una base 0.7.2 ya publicada basta con una ejecución de
**Construir índice**. El cambio de versión del extractor hace que las fichas se
calculen otra vez a partir del texto fuente conservado en SQLite; no es
necesario volver a descargar los PDF que ya están representados en la base. La
misma ejecución crea el nuevo índice semántico, verifica los paquetes y realiza
el commit que activa el redespliegue de Streamlit.

La actualización de 0.8.0 a 0.9.0 no cambia el esquema ni el contenido del
índice. Si esa reconstrucción ya se completó, basta con actualizar el código,
esperar **Pruebas** y el redespliegue de Streamlit; no se debe reconstruir la
base por el rediseño de interfaz.

La 0.9.1 sí cambia el formato interno del índice semántico para permitir
checkpoint, deduplicación y reutilización segura. Si el índice neuronal de la
0.9.0 quedó incompleto, ejecuta **Construir índice** una sola vez después de
subir esta corrección. El workflow continuará por sí mismo y solo publicará la
nueva base cuando esté completa.

Ya no existe una casilla `full_rebuild` en el workflow normal. Esto evita que
una reconstrucción total pueda iniciarse accidentalmente desde la acción
destinada a incorporar actas nuevas.

## Configuración sin IA generativa

La versión 0.9.1 se opera con `LLM_PROVIDER = "prompt_only"`. El buscador recupera
fuentes sin enviar la consulta a un LLM y el complemento semántico se ejecuta
localmente. No hace falta configurar una clave de OpenAI ni de Azure OpenAI.

### Índice neuronal multilingüe

El backend predeterminado es `ACTAS_SEMANTIC_BACKEND=neural`. Usa
`fastembed==0.8.0` y el modelo ONNX cuantizado
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` del registro
versionado `fastembed-0.8.0-registry`. Tanto el modelo como su registro forman
parte de la firma del índice, de modo que un cambio obliga a reconstruirlo. Los
vectores se normalizan y se guardan cuantizados a `int8` dentro de
`semantic.db`.

La consulta comprueba que la huella exacta de IDs, relaciones y texto de
`actas.db` coincida con la registrada en `semantic.db`. Si no coincide, no usa
los vectores y vuelve automáticamente a FTS5. Si FastEmbed o el modelo no pueden
cargarse, conserva el reranking semántico determinístico anterior. Se puede
forzar esa ruta ligera con `ACTAS_SEMANTIC_BACKEND=local`.

El primer uso tras un despliegue puede tardar mientras FastEmbed descarga el
modelo de aproximadamente 0,22 GB; ninguna consulta se envía a un servicio de
inferencia. GitHub Actions conserva el modelo en caché. La construcción también
mantiene un checkpoint separado del índice publicado y confirma los embeddings
por lotes. Si un segmento se interrumpe después de un checkpoint válido, una
nueva ejecución puede continuar desde ese punto sin repetir los lotes ya
confirmados.

`semantic.checkpoint.db` es una base temporal de trabajo, no un índice para
Streamlit ni un archivo que deba subirse manualmente. `semantic-progress.json`
describe el avance de la cadena. Durante una construcción incompleta ambos se
conservan en Actions Cache; al llegar al 100 %, el checkpoint se valida, se
promueve atómicamente a `semantic.db` y el reporte de progreso se incorpora al
commit final.

Los embeddings se almacenan una vez por SHA-256 del texto exacto. Esto evita
calcular dos veces fragmentos idénticos y permite que la siguiente actualización
reutilice todos los textos que no cambiaron. La capa TF-IDF/distribucional sí se
recalcula para representar el corpus vigente, pero normalmente la inferencia
neuronal costosa queda limitada a los textos nuevos o modificados.

La página de Administración exige `ADMIN_PASSWORD`. Si no se configura, queda
deshabilitada y el índice solo puede construirse mediante `python build_index.py`.

`.streamlit/secrets.toml` está excluido de Git y nunca debe subirse al
repositorio. `ADMIN_PASSWORD` debe reemplazarse por una contraseña privada si se
habilita la página de Administración.

## Despliegue en Streamlit Community Cloud

1. Crear un repositorio privado vacío en GitHub.
2. Subir el contenido de este proyecto.
3. Crear una aplicación en Streamlit Community Cloud usando `home.py`.
4. Configurar los secretos desde el panel de Streamlit, nunca en GitHub.
5. Esperar que **Pruebas** termine en verde.
6. Ejecutar una vez **Actions → Construir índice → Run workflow**.
7. Seguir la cadena en **Actions** sin iniciar ejecuciones adicionales. Cada
   segmento guarda su avance y programa el siguiente automáticamente.
8. Esperar que el último resumen indique estado completo, cero pendientes y el
   commit automático que activa el nuevo despliegue de Streamlit.

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

Una vez completada la primera base 0.9.1, las ejecuciones posteriores reutilizan
los embeddings cuyos textos conservan el mismo SHA-256. Por ello, la incorporación
de una nueva acta normalmente requiere inferencia neuronal solo para sus
fragmentos nuevos o para fragmentos cuyo texto haya cambiado; no vuelve a
procesar todo el histórico.

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
- avance confirmado de la capa neuronal y cantidad de embeddings pendientes;
- tamaño de la base compactada.

El esquema 6 conserva una copia comprimida del texto fuente por página para
permitir reextracciones auditables, además de los fragmentos necesarios para
buscar. El paquete
`data/actas.db.package.json` registra tamaño y SHA-256 de la base y de cada
fragmento, y la aplicación los valida antes de usar el índice. El índice
semántico se distribuye del mismo modo mediante
`data/semantic.db.package.json`. Un `semantic.checkpoint.db` incompleto nunca se
incluye en ese paquete ni reemplaza la base semántica publicada.

La extracción regulatoria es deliberadamente conservadora. Una ficha puede
estar incompleta o mal clasificada por diferencias históricas de formato; por
eso se marca como automática y siempre debe comprobarse contra la página del
PDF mostrada en el visor.

## Comparación y trazabilidad

Desde el **Explorador** se pueden marcar evidencias y enviarlas a **Comparar**.
También es posible buscar fichas directamente en esa página, agregarlas a la
selección, recorrer los resultados por páginas y ver qué campos difieren antes
de abrir la evidencia en el visor. La misma página construye cronologías con
ejes y filtros ampliados, informa el total y permite descargar un CSV compatible
con Excel y un reporte imprimible.

## Verificación

Las pruebas unitarias no requieren Streamlit:

```powershell
python -m unittest discover -v
```

Antes de publicar una versión se debe comprobar:

- que cada resultado abre el documento correcto;
- que el número de página coincide con el PDF;
- que no se registran preguntas, secretos ni información corporativa;
- que el modo neuronal ordena resultados y que su ruta de respaldo sigue
  respondiendo si el modelo local no está disponible;
- que la comparación resalta diferencias y la cronología pagina sin duplicar
  fichas;
- que el visor abre la evidencia correcta y permite navegar, ampliar, rotar,
  buscar y copiar texto;
- que las páginas recuperadas con OCR sean legibles y que las restantes queden
  identificadas como candidatas para comprobación documental.

El flujo `.github/workflows/tests.yml` repite automáticamente la compilación y
las pruebas con Python 3.12 después de cada cambio enviado a GitHub. Desde la
versión 0.8.0 se usa directamente la API `pymupdf`, sin depender del nombre heredado
`fitz`, y Tesseract se instala dentro del runner de GitHub. La búsqueda
neuronal usa un modelo multilingüe local a través de FastEmbed y conserva el
índice semántico determinístico como respaldo. No requiere instalar herramientas
de desarrollo en el computador corporativo.

## Propiedad y distribución

Repositorio privado. La titularidad, licencia y política de distribución deben
definirse antes de compartir el código fuera del equipo autorizado.
