# Alcance cerrado de la versión 0.10.0

## Objetivo

Mejorar la recuperación y explotación del corpus de actas INVIMA sin incorporar
IA generativa, sin alterar el carácter documental de la herramienta y sin exigir
una plataforma distinta de Streamlit para el piloto.

## Imprescindible

1. **Búsqueda híbrida global.** Reunir candidatos de FTS5, coincidencia literal
   y recuperación neuronal sobre todo el corpus antes de fusionar y ordenar los
   resultados. Una consulta híbrida no puede limitar el componente neuronal a
   los candidatos textuales ya encontrados.
2. **Índice ANN persistente.** Construir `semantic-ann.db` desde los embeddings
   completos existentes, consultar candidatos aproximados y volver a puntuar
   esos candidatos con sus vectores originales. El índice debe ser local,
   versionado, verificable y compatible con el empaquetado en partes del
   repositorio privado.
3. **Búsqueda por campo regulatorio.** Permitir limitar la consulta, como mínimo,
   a solicitud/indicación, concepto, producto, principio activo, interesado,
   expediente y radicado. El alcance seleccionado debe verse en la interfaz y
   en las exportaciones.
4. **Transparencia del motor.** Informar qué rutas participaron realmente
   —textual, literal de página, neuronal ANN o respaldo—, la disponibilidad de
   cada índice y el motivo de una degradación. La mera existencia de
   `semantic.db` no basta para afirmar que la consulta fue neuronal.
5. **Frases sobre la página completa.** Confirmar las frases literales en el
   texto fuente de la página para encontrar también una secuencia que atraviese
   el límite entre dos fragmentos, manteniendo la página y el PDF como evidencia.
6. **Paginación y totales en la capa de datos.** Evitar el límite previo de 360
   fragmentos antes de agrupar por acta. La navegación debe ser estable; los
   totales exactos se distinguen expresamente de los conjuntos acotados propios
   de una recuperación semántica.
7. **Exportación de resultados.** Descargar CSV y XLSX con la consulta, el modo,
   el campo, los filtros, los puntajes disponibles, acta, página, fragmento y URL
   oficial. La exportación no debe convertir valores en fórmulas de hoja de
   cálculo.
8. **Migración y despliegue compatibles.** Migrar `actas.db` de forma aditiva al
   esquema 7, construir y verificar los tres paquetes (`actas.db`, `semantic.db`
   y `semantic-ann.db`) y mantener operativa la aplicación durante el despliegue.
9. **Pruebas de regresión.** Cubrir fusión global, ANN y respaldo, campos,
   frases entre fragmentos, paginación, totales, exportaciones y arranque con
   bases de la versión anterior.

## Deseable, si no compromete lo imprescindible

- Métricas en el resumen de GitHub Actions sobre tamaño, tiempo de construcción
  y cantidad de elementos del ANN.
- Controles de diagnóstico discretos para copiar la configuración efectiva de
  una búsqueda sin exponer secretos ni términos de otros usuarios.
- Ajustes de accesibilidad en filtros, insignias del motor y botones de descarga.
- Parámetros internos documentados para ajustar el número de candidatos ANN sin
  cambiar el comportamiento funcional de la aplicación.

Los deseables no bloquean la entrega ni justifican retrasar o debilitar un punto
imprescindible.

## Fuera de alcance

- Consulta o síntesis mediante un LLM, claves de OpenAI/Azure y generación de
  respuestas libres.
- Monitor del Decreto de Transparencia.
- Módulos de evaluación, corrección o calificación de las actas.
- Sustitución de Streamlit por un frontend independiente.
- Migración del piloto a PostgreSQL, un motor vectorial administrado o servicios
  externos de inferencia.
- Revisión humana masiva del contenido publicado por INVIMA.

## Compatibilidad y despliegue

1. Subir el overlay de código sin borrar `data/`, `documents_manifest.csv` ni
   `actas_catalog.csv`.
2. Esperar que **Pruebas** termine en verde.
3. Ejecutar una sola vez **Actions → Construir índice → Run workflow**.
4. Esperar el commit automático que contenga los manifiestos y partes de
   `actas.db`, `semantic.db` y `semantic-ann.db`.
5. Confirmar el redespliegue de Streamlit y la versión `0.10.0`.

Antes del paso 4, la aplicación debe arrancar contra la publicación anterior.
Si todavía no están el esquema 7 o el paquete ANN, conserva las búsquedas
compatibles, identifica la ruta de respaldo y no muestra como disponibles las
funciones que dependen de esos componentes.

La automatización de nuevas actas no cambia: **Actualizar catálogo desde
INVIMA** continúa ejecutándose de lunes a viernes y dispara la construcción
incremental solo cuando encuentra novedades o documentos pendientes. La
configuración recomendada sigue siendo:

```toml
LLM_PROVIDER = "prompt_only"
```

## Criterio de cierre

La versión se considera terminada cuando las pruebas pasan, la migración desde
una base 0.9.2 conserva las consultas previas, el workflow publica y verifica
los tres paquetes, y las siete capacidades funcionales imprescindibles pueden
comprobarse desde la aplicación con trazabilidad a la página oficial.
