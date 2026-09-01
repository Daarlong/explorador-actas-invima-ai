# Explorador de Actas INVIMA + Analista IA

Aplicación privada en Streamlit para explorar actas públicas del INVIMA y
formular preguntas con respuestas sustentadas en documento y página.

Este proyecto conserva las mejores ideas del prototipo
`pdf-search-nn-streamlit` —manifiesto de URLs, descarga de PDFs, extracción por
página y preparación de contexto— y reemplaza el índice JSON por SQLite FTS5,
filtros estructurados y una integración opcional con un proveedor de IA
aprobado.

## Alcance

- Corpus configurado desde 2020 hasta 2025 y 2026 hasta el Acta 08.
- 179 documentos, contando por separado cada parte publicada.
- Explorador de actas con búsqueda textual y frases exactas.
- Filtros por año, número de acta, sala/sección y parte.
- Evidencia con título, página, fragmento y URL de origen claramente identificada.
- Analista IA con citas `[F#]` y negativa cuando no hay evidencia.
- Modo `prompt_only` para trabajar sin enviar consultas a servicios externos.
- Compatibilidad opcional con OpenAI o Azure OpenAI.
- Administración del manifiesto e indexación desde la interfaz o por CLI.

No contiene funcionalidades relacionadas con un monitor de transparencia.

### Cobertura documental

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

## Estructura

```text
.
├── home.py
├── pages/
│   ├── 1_Explorador.py
│   ├── 2_Analista_IA.py
│   └── 3_Administracion.py
├── services/
│   ├── database.py
│   ├── downloader.py
│   ├── indexing.py
│   ├── llm.py
│   ├── manifest.py
│   ├── metadata.py
│   ├── pdf_reader.py
│   ├── retrieval.py
│   └── text_utils.py
├── tests/
├── documents_manifest.csv
├── build_index.py
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

Si no puedes instalar dependencias localmente, abre **Actions → Construir
índice → Run workflow** en GitHub. El flujo:

1. valida el catálogo y ejecuta las pruebas;
2. descarga e indexa los 179 documentos;
3. comprime y divide la base en fragmentos menores de 100 MB;
4. guarda esos fragmentos automáticamente en el repositorio privado.

No es necesario descargar un artefacto ni subir manualmente `actas.db`.

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
5. Ejecutar una vez **Actions → Construir índice → Run workflow**.
6. Esperar el nuevo despliegue automático de Streamlit.

El sistema de archivos de una aplicación alojada no debe considerarse una base
de datos permanente. Para el piloto, la acción versiona el índice comprimido y
la aplicación lo reconstruye en su directorio temporal al iniciar. Para una
etapa corporativa se recomienda PostgreSQL, Azure Database o un servicio de
búsqueda aprobado.

## Actualización del corpus

`documents_manifest.csv` acepta como mínimo:

```csv
title,url
Acta No 01 de 2026 SEMPB Primera Parte,https://www.invima.gov.co/...
```

También admite las columnas opcionales `year`, `acta_number`, `section`, `part`
y `source_type`. Si no existen los metadatos documentales, la aplicación
intenta obtenerlos del título.

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
las pruebas con Python 3.12 después de cada cambio enviado a GitHub. No requiere
instalar herramientas de desarrollo en el computador corporativo.

## Propiedad y distribución

Repositorio privado. La titularidad, licencia y política de distribución deben
definirse antes de compartir el código fuera del equipo autorizado.
