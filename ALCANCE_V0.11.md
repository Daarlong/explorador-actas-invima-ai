# Alcance cerrado de la versión 0.11.0

## Objetivo

Convertir el corpus ya indexado en una herramienta más clara para consulta y
análisis exploratorio: comprobar técnicamente que la publicación funciona sobre
la base real, reconocer terminología regulatoria equivalente, mostrar conteos
globales junto a los filtros y ofrecer un tablero descriptivo trazable. La
versión no interpreta, califica ni corrige el contenido de las actas.

## Imprescindible

1. **Validación técnica sobre el corpus publicado.** Ejecutar una batería
   reproducible contra las bases reales para comprobar, como mínimo, apertura
   de SQLite, disponibilidad de FTS5 y ANN, cobertura temporal, búsquedas
   textuales, literales, por campo e híbridas, paginación y exportación. Cada
   comprobación debe dejar un resultado comprensible y evidencia suficiente
   para diagnosticar un fallo técnico. El flujo manual e independiente
   **Validar búsquedas publicadas** solo debe fallar por contratos técnicos;
   ejemplos explícitos de relevancia se informan como observaciones y no
   bloquean una publicación.
2. **Separación entre validación técnica y evaluación documental.** Las pruebas
   confirman que los mecanismos responden y que las fuentes siguen siendo
   trazables; no asignan una nota de calidad a las actas, no deciden si INVIMA
   actuó correctamente y no convierten los campos extraídos en una verdad
   distinta del documento oficial.
3. **Diccionario regulatorio auditable.** Mantener grupos locales y versionados
   de sinónimos, siglas, variantes ortográficas y expresiones regulatorias
   equivalentes. La consulta original siempre se conserva y la expansión
   aplicada debe poder verse en la traza de búsqueda. No se inventan
   equivalencias entre productos ni principios activos específicos.
4. **Expansión segura de consultas.** Aplicar el diccionario en tiempo de
   consulta sin reconstruir `actas.db`, `semantic.db` ni `semantic-ann.db`. No
   expandir una frase exigida como literal ni identificadores como expedientes,
   radicados o números de registro. Una expansión nunca puede ocultar la
   coincidencia con los términos escritos por el usuario.
5. **Facetas con conteos globales.** Mostrar distribuciones por año, serie o
   sala, resultado, tipo de solicitud, principio activo, interesado o titular y
   producto. Los valores cuentan actas distintas del conjunto completo de una
   búsqueda, no fragmentos ni únicamente la página visible. Cuando el motor no
   permita un universo exhaustivo, la interfaz debe declarar que el conteo es
   acotado en vez de presentarlo como exacto.
6. **Filtros coherentes con las facetas.** Permitir aplicar una faceta sin
   perder la consulta ni los demás filtros, recalcular los conteos de forma
   consistente y evitar contar varias veces una misma acta por contener varios
   fragmentos coincidentes. Los estados sin datos y sin coincidencias deben ser
   explícitos.
7. **Tablero analítico descriptivo.** Incorporar una vista agregada del corpus
   con métricas separadas de documentos, actas únicas y fichas extraídas;
   tendencia anual; desgloses por resultado, tipo de solicitud, principio
   activo e interesado; y cobertura de extracción total y por campo. Las
   métricas deben indicar qué entidad cuentan y no mezclar documentos, páginas,
   fragmentos y fichas. Todo límite o truncamiento debe quedar visible.
8. **Trazabilidad desde las agregaciones.** Una selección del tablero o de una
   faceta debe permitir llegar mediante un detalle paginado al conjunto
   documental correspondiente y desde allí al acta, numeral, página o rango y
   PDF oficial disponibles. Ninguna gráfica debe sustituir la evidencia
   primaria.
9. **Compatibilidad y regresión.** Las mejoras deben operar sobre las bases
   publicadas por la versión 0.10.0, conservar la búsqueda y la actualización
   automática de actas, respetar los temas claro y oscuro y cubrirse con pruebas
   que no dependan de servicios externos.

## Deseable, si no compromete lo imprescindible

- Exportar a CSV las tablas agregadas del tablero con sus filtros y fecha de
  generación.
- Mostrar la versión del diccionario y los términos expandidos en el detalle
  técnico de cada búsqueda.
- Incluir ejemplos de consultas regulatorias conocidas que faciliten la prueba
  funcional posterior al despliegue.
- Presentar estados vacíos y ayudas breves que distingan tendencia descriptiva
  de conclusión regulatoria.
- Mantener tiempos interactivos razonables mediante consultas agregadas,
  límites visibles y caché de resultados que no altere los conteos.

Los deseables no bloquean la entrega ni justifican debilitar la trazabilidad o
presentar estimaciones como totales exactos.

## Fuera de alcance

- Consulta, resumen o redacción mediante un LLM y configuración de claves de
  OpenAI, Azure OpenAI u otro proveedor generativo.
- Monitor del Decreto de Transparencia o seguimiento de otros portales.
- Módulos de evaluación, corrección, calificación o revisión humana de actas.
- Diagnósticos jurídicos, clínicos o regulatorios generados automáticamente.
- Predicciones, recomendaciones de aprobación o comparación de desempeño de
  empresas.
- Sustitución de Streamlit por un frontend independiente.
- Migración a PostgreSQL, un almacén vectorial administrado o una plataforma de
  inteligencia empresarial externa.
- Recalcular embeddings o volver a descargar el histórico únicamente para
  activar estas capacidades.

## Compatibilidad y despliegue

La 0.11.0 es una actualización de código sobre una publicación 0.10.0 completa.
El diccionario se aplica al consultar y las facetas y el tablero leen las
estructuras existentes; por ello la actualización ordinaria no debe reconstruir
las bases ni volver a calcular los 321.246 embeddings ya publicados.

1. Subir el overlay sin borrar `data/`, `documents_manifest.csv` ni
   `actas_catalog.csv`.
2. Esperar que **Actions → Pruebas** termine en verde.
3. Dejar que Streamlit se redespliegue y confirmar la versión `0.11.0`.
4. Ejecutar **Actions → Validar búsquedas publicadas → Run workflow** y revisar
   su resumen técnico y sus observaciones informativas.
5. Hacer una prueba funcional de sinónimos, facetas y navegación desde el
   tablero hasta la evidencia oficial.

No se debe pulsar **Construir índice** solo por instalar esta versión. Ese
workflow continúa reservado para una nueva acta, un documento pendiente o una
modificación futura que sí cambie los índices. La automatización de
**Actualizar catálogo desde INVIMA** permanece activa y sigue generando los
tres paquetes SQLite cuando detecta novedades.

La configuración recomendada continúa siendo:

```toml
LLM_PROVIDER = "prompt_only"
```

## Criterio de cierre

La versión se considera terminada cuando las pruebas unitarias pasan, la
validación técnica puede ejecutarse contra una publicación 0.10.0 sin modificar
las actas, el diccionario amplía únicamente consultas seguras y deja traza, las
facetas describen el conjunto global con su carácter exacto o acotado, y el
tablero permite navegar desde cada agregado hasta fuentes consultables. Ningún
flujo visible debe presentarse como evaluación o corrección del insumo oficial.
