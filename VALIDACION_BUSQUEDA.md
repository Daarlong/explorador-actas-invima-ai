# Validación técnica del corpus publicado

`validate_search.py` comprueba que las bases ya publicadas pueden consultarse
de extremo a extremo. No es un módulo de evaluación, no juzga el contenido de
las decisiones y no modifica las actas, las fichas ni los paquetes del
repositorio.

## Ejecución recomendada

En GitHub abre **Actions → Validar búsquedas publicadas → Run workflow**. El
workflow restaura `actas.db`, `semantic.db` y `semantic-ann.db`, ejecuta las
sondas y deja dos salidas:

- un resumen legible en la página de la ejecución;
- el artefacto `search-validation-report-<run-id>` con el JSON completo.

Es un workflow manual e independiente de construcción y despliegue. No hace
commits, no publica bases y no activa Streamlit. Un estado rojo significa que
falló un contrato técnico de esa validación; no revierte ni bloquea la versión
que ya está desplegada.

Además, **Construir índice** ejecuta automáticamente las mismas sondas
estructurales sobre las tres bases restauradas de la publicación candidata,
después de crear el ANN y antes del commit automático. El resultado aparece en
el Summary, por lo que para una actualización normal no es necesario descargar
ningún artefacto ni iniciar la validación manual. En ese control automático no
se ejecutan casos explícitos orientativos.

## Qué comprueba

- integridad SQLite, versión del esquema, tablas, filas mínimas y relaciones;
- cobertura completa de las filas indexables de `documents_manifest.csv`, con
  rango temporal, faltantes, documentos no consultables, adicionales y
  porcentaje de cobertura;
- cardinalidad exacta entre fragmentos/fichas y sus respectivos índices FTS;
- presencia, tamaños y huellas SHA-256 de los tres paquetes divididos;
- correspondencia de `semantic.db` con `actas.db` y del ANN con sus embeddings;
- cobertura temporal mínima y máxima configurada para el corpus;
- búsqueda textual global y evidencia anclada a documento, página y fragmento;
- recuperación híbrida real, confirmando que se usaron el modelo neuronal y ANN;
- frases literales confirmadas contra la página fuente;
- búsquedas por campos persistidos, sin asumir ningún medicamento concreto;
- paginación textual exacta, estable y sin repetir documentos entre páginas.
- generación en memoria de CSV y XLSX, con columnas y evidencia básicas.

Las sondas obligatorias se construyen con texto y valores que ya existen en la
base. Esto evita convertir ejemplos regulatorios inventados en criterios de
aprobación.

La cobertura documental toma las filas del manifiesto con URL y año igual o
posterior al inicio configurado. Compara primero por `catalog_id` y, cuando no
está disponible en alguno de los lados, por `manifest_url`. Cualquier faltante
es un bloqueo técnico. Para considerarse cubierto, el documento debe tener el
inventario de páginas completo, al menos una página indexada y texto buscable
en `pages`/`chunks`: una fila vacía en `documents` no certifica cobertura. Los
documentos adicionales quedan identificados como observación para no borrar
automáticamente históricos válidos.

El manifiesto también tiene su propio contrato temporal, independiente del
rango que declare la base. La configuración oficial exige que el inventario
indexable empiece a más tardar en 2013, llegue por lo menos a 2026 y contenga
14 años distintos. El rango real del CSV queda visible en el Summary. Así, un
manifiesto recortado a 2020–2026 no puede certificar falsamente una base igual
de recortada aunque todas sus filas estén presentes.

## Configuración

`search-validation-cases.json` permite ajustar la versión de esquema, tablas,
rango y cantidad mínima de años tanto de la base como del manifiesto,
verificación SHA-256, campos, recuperación híbrida, formatos de exportación y
tamaño de las sondas. Los controles temporales del manifiesto son opcionales
para configuraciones antiguas; la configuración oficial los activa. También
acepta `explicit_cases` para consultas conocidas:

```json
{
  "name": "Consulta interna conocida",
  "query": "texto que se desea observar",
  "mode": "textual",
  "field_scope": "all",
  "exact_phrase": false,
  "minimum_results": 1
}
```

Los casos explícitos son siempre **orientativos**: si no alcanzan el mínimo se
registran como advertencia y no cambian el código de salida. Solo los contratos
estructurales y de trazabilidad pueden hacer fallar la validación.

También puede ejecutarse localmente después de restaurar las bases:

```bash
python validate_search.py \
  --config search-validation-cases.json \
  --manifest documents_manifest.csv \
  --report search-validation-report.json
```
