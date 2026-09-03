# Alcance cerrado de la versión 0.7.1

La versión 0.7.1 corrige y hace auditable la estructuración masiva del corpus
histórico. No incorpora nuevas funciones de IA ni realiza llamadas a modelos
externos.

## Imprescindible

1. **Fuente por página preservada**
   - Guardar el texto extraído de cada página conservando saltos de línea.
   - Registrar si proviene del PDF o de OCR, versión del extractor, calidad y
     error de extracción.
   - Mantener compatibilidad de lectura con los índices anteriores.

2. **Reprocesamiento histórico seguro**
   - Crear una base candidata separada de la base publicada.
   - Ofrecer dos modos inequívocos en GitHub Actions: `Diagnóstico` y
     `Publicar`.
   - Reutilizar PDF en caché, descargar solo los faltantes y publicar únicamente
     después de superar las validaciones.
   - Preservar el registro de correcciones humanas.

3. **Extractor regulatorio v5**
   - Reconocer estructuras históricas de numerales y rótulos.
   - Extraer productos e ingredientes desde `Composición`, `IFA`, `DCI`,
     `Principio activo` y expresiones del tipo `Cada ... contiene`.
   - Conservar por separado los ingredientes de combinaciones, sin inferir
     equivalencias farmacológicas ni relacionar marcas por conocimiento externo.

4. **Procedencia por campo**
   - Guardar para cada valor automático el texto literal, forma normalizada,
     página, fragmento, método y confianza.
   - Mostrar esa evidencia en la interfaz y distinguirla de una corrección
     humana.

5. **Identidad y revisiones estables**
   - Conservar el identificador de una decisión al reprocesar cuando exista una
     coincidencia única y segura.
   - Resolver masivamente identificadores repetidos mediante coincidencias
     uno-a-uno que combinen página, contenido, campos regulatorios y orden.
   - Si no existe una correspondencia segura y ningún UID involucrado tiene
     revisión humana, asignar explícitamente una identidad nueva y auditarla
     como `not_inherited`; nunca heredar un UID al azar.
   - Detectar revisiones huérfanas o coincidencias ambiguas que puedan afectar
     un UID revisado y bloquear la publicación hasta resolverlas.

6. **Valor vigente único**
   - Aplicar las correcciones humanas de forma consistente en búsqueda,
     filtros, fichas, comparación, cronología y exportaciones.
   - Presentar campos vacíos como `No extraído` y permitir filtrar las fichas a
     las que les falta principio activo.

7. **Cronología híbrida verificable**
   - Combinar fichas corregidas, fichas estructuradas y menciones textuales.
   - Etiquetar el origen de cada coincidencia y mostrar la página y el
     fragmento que la sustentan.
   - No presentar una mención textual como un campo confirmado.

8. **Controles de publicación**
   - Medir completitud de numeral, producto, principio activo, interesado,
     expediente, radicado, identificadores, rango de páginas, concepto y
     resultado clasificado.
   - Comparar la candidata con la base vigente y bloquear regresiones por encima
     del umbral definido.
   - Verificar integridad SQLite, paquetes comprimidos restaurables y ausencia
     de revisiones huérfanas o ambiguas.
   - Mantener pruebas sintéticas reproducibles y admitir un banco oro humano sin
     inventar resultados esperados.

## Deseable, no bloqueante para 0.7.1

- OCR avanzado para páginas con texto parcial o de muy baja calidad.
- Fusión semántica global y reranking del buscador híbrido.
- Verificación de la huella del índice semántico durante cada inicio de la app.
- Paginación SQL completa para conjuntos de resultados muy grandes.
- Detección de sustitución de un PDF conservando la misma URL.
- Revisión humana en lote y almacenamiento externo persistente del historial.
- SSO corporativo y almacenamiento externo de los índices.

## Fuera de alcance

- Consulta mediante IA, RAG, prompts nuevos o conexión a un LLM.
- Embeddings externos.
- Monitor de transparencia o seguimiento de trámites.
- Inferencias clínicas o farmacológicas que no estén escritas en el acta.

## Criterio de finalización

La versión queda lista cuando las pruebas automatizadas pasan, el flujo de
`Diagnóstico` genera informes sin modificar la base publicada y el flujo de
`Publicar` solo permite reemplazarla después de superar todos los controles.
