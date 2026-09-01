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
- Explorador de actas con búsqueda textual y frases exactas.
- Filtros por año, número de acta, sala/sección y parte.
- Evidencia con título, página, fragmento y URL de origen claramente identificada.
- Analista IA con citas `[F#]` y negativa cuando no hay evidencia.
- Modo `prompt_only` para trabajar sin enviar consultas a servicios externos.
- Compatibilidad opcional con OpenAI o Azure OpenAI.
- Administración del manifiesto e indexación desde la interfaz o por CLI.
- Índice SQLite compacto, empaquetado en partes verificadas con SHA-256.
- Informe de integridad visible desde la aplicación.
- Actualización incremental para descargar e indexar únicamente actas nuevas.
- Página de Catálogo con estado indexado, pendiente, sin enlace o ya no listado.

No contiene funcionalidades relacionadas con un monitor de transparencia.

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
│   └── 5_Catalogo.py
├── services/
│   ├── catalog.py
│   ├── database.py
│   ├── downloader.py
│   ├── indexing.py
│   ├── integrity.py
│   ├── llm.py
│   ├── manifest.py
│   ├── metadata.py
│   ├── pdf_reader.py
│   ├── retrieval.py
│   └── text_utils.py
├── tests/
├── actas_catalog.csv
├── documents_manifest.csv
├── build_index.py
├── package_index.py
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
   publicaciones nuevas y evita duplicar los enlaces repetidos en la página.
2. Revisa la página **Catálogo** de la aplicación.
3. Abre **Actions → Construir índice → Run workflow** para incorporar al
   explorador los registros que figuren como pendientes.

El flujo de construcción del índice:

1. valida el catálogo y ejecuta las pruebas;
2. restaura el índice anterior cuando existe;
3. descarga e indexa únicamente documentos nuevos;
4. genera un informe de integridad;
5. comprime la base, calcula hashes y la divide en fragmentos de 90 MiB;
6. guarda el índice y el informe automáticamente en el repositorio privado.

No es necesario descargar un artefacto ni subir manualmente `actas.db`.

El workflow de catálogo se ejecuta además automáticamente de lunes a viernes a
las 18:30, hora de Colombia. Solo realiza un commit cuando detecta un cambio.

La primera construcción después de ampliar el índice a 2013 puede tardar varias
horas y aumentar considerablemente el tamaño de la base. El workflow dispone de
un máximo de seis horas, conserva los PDF descargados en caché y deja los
documentos fallidos pendientes para reintentarlos sin perder los correctos.

La primera ejecución de la versión 0.2.1 o posterior detecta el índice anterior y migra sus
fragmentos al esquema compacto sin volver a descargar los 179 PDF. Después, las
ejecuciones normales son incrementales. Selecciona `full_rebuild` únicamente
cuando necesites volver a procesar todos los documentos y obtener un inventario
exacto de todas las páginas sin texto.

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
6. Ejecutar una vez **Actions → Construir índice → Run workflow**.
7. Esperar el nuevo despliegue automático de Streamlit.

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

Para actualizar automáticamente, usa `python sync_catalog.py` o el workflow
**Actualizar catálogo desde INVIMA**.

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
para trasladar el cambio al catálogo y luego **Construir índice** sin activar
`full_rebuild`.

## Integridad y tamaño del índice

La página **Integridad** muestra:

- documentos esperados e indexados;
- páginas con texto y fragmentos consultables;
- consistencia interna de SQLite y del índice FTS5;
- documentos faltantes o inesperados;
- páginas sin texto que podrían necesitar OCR;
- tamaño de la base compactada.

El esquema compacto evita guardar el texto completo en páginas, fragmentos,
texto normalizado y contenido FTS al mismo tiempo. El paquete
`data/actas.db.package.json` registra tamaño y SHA-256 de la base y de cada
fragmento, y la aplicación los valida antes de usar el índice.

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
- que los documentos sin texto se identifican para un futuro proceso de OCR.

El flujo `.github/workflows/tests.yml` repite automáticamente la compilación y
las pruebas con Python 3.12 después de cada cambio enviado a GitHub. La versión
0.3.1 usa directamente la API `pymupdf`, sin depender del nombre heredado
`fitz`. No requiere instalar herramientas de desarrollo en el computador
corporativo.

## Propiedad y distribución

Repositorio privado. La titularidad, licencia y política de distribución deben
definirse antes de compartir el código fuera del equipo autorizado.
