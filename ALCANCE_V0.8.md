# Alcance cerrado de la versión 0.8.0

La versión 0.8.0 mejora tres capacidades del explorador: extracción estructurada
masiva, búsqueda semántica y análisis comparativo de evidencias. El PDF oficial
y la página citada continúan siendo la fuente de trabajo.

## Imprescindible

1. **Extracción masiva mejorada**
   - Ampliar el reconocimiento de estructuras y rótulos históricos sin cambiar
     el texto oficial.
   - Extraer producto, composición, principio activo, tipo de solicitud,
     concepto y resultado mediante reglas trazables.
   - Recalcular automáticamente las fichas afectadas a partir del texto fuente
     por página ya almacenado.
   - Evitar la descarga repetida de PDF cuando la fuente correspondiente ya
     existe en la base.
   - Conservar valor literal, valor normalizado, página, fragmento, método y
     confianza para cada campo derivado.

2. **Semántica neuronal multilingüe local**
   - Incorporar representaciones neuronales multilingües ejecutadas localmente,
     sin enviar las consultas a una API externa.
   - Cuantizar y guardar las representaciones en el índice semántico SQLite.
   - Usar la señal neuronal para ordenar candidatos recuperados de manera
     verificable.
   - Conservar la búsqueda semántica determinística y FTS5 como rutas de
     respaldo cuando el modelo o su dependencia no estén disponibles.
   - Verificar que el índice semántico corresponda a la base documental activa.

3. **Comparación operable desde su propia página**
   - Buscar fichas directamente en **Comparar**, sin exigir una selección previa
     en el Explorador.
   - Mostrar el total, paginar los candidatos y permitir agregarlos a la
     comparación.
   - Mostrar la matriz completa y un resumen de los campos diferentes o no
     extraídos, sin inferir cuál valor es correcto.
   - Abrir desde cada ficha el PDF en la página que sustenta la evidencia.

4. **Cronología ampliada**
   - Construir cronologías por producto, principio activo, expediente, radicado,
     interesado, resultado y tipo de solicitud.
   - Añadir filtros útiles, total de coincidencias y paginación.
   - Mantener separadas las fichas estructuradas y las menciones textuales.
   - Conservar enlace, página y fragmento para cada evento.

5. **Visor documental mejorado**
   - Abrir la página exacta desde Explorador, el asistente de fuentes y Comparar.
   - Permitir salto de página, zoom, rotación, navegación anterior/siguiente,
     búsqueda dentro del texto disponible y copia o descarga de ese texto.
   - Identificar el PDF de caché por su origen y comprobar su huella antes de
     reutilizarlo.
   - Mostrar claramente cuándo solo existe un fragmento indexado y no el texto
     completo de la página.

6. **Entrega y actualización seguras**
   - Distribuir la versión como overlay sin sobrescribir el catálogo, el
     manifiesto ni las bases publicadas del repositorio privado.
   - Ejecutar pruebas automáticas antes de reconstruir.
   - Actualizar extracción e índice semántico con una sola ejecución de
     **Construir índice**.
   - Validar integridad y paquetes antes del commit que activa el redespliegue
     de Streamlit.

## Deseable, no bloqueante para 0.8.0

- Caché persistente del modelo local entre todas las plataformas de despliegue.
- Guardar comparaciones y filtros favoritos por usuario.
- Exportar una cronología directamente como PDF maquetado.
- Diseño específico para pantallas móviles pequeñas.
- Medición posterior de calidad con un método acordado por usuarios del área,
  sin convertirla en un módulo de la aplicación.

## Fuera de alcance

- Módulo de evaluación, banco de casos, calificación humana o métricas
  Hit@K/MRR visibles en la aplicación.
- Módulo de corrección o revisión manual de fichas.
- Consulta mediante IA generativa, RAG, nuevos prompts o conexión operativa con
  un LLM.
- Envío de consultas o fragmentos a un proveedor externo de embeddings.
- Corrección del contenido de las actas oficiales.
- Inferencias clínicas o farmacológicas no escritas en el documento.
- Monitor de transparencia o seguimiento de trámites.
- Sustitución del almacenamiento actual por PostgreSQL, SSO corporativo o una
  infraestructura productiva nueva.

## Criterio de finalización

La versión queda lista cuando las pruebas automatizadas pasan; una ejecución de
**Construir índice** reprocesa las fichas desde el texto fuente, construye y
valida el índice semántico con su ruta de respaldo, publica paquetes íntegros y
la aplicación permite buscar, comparar, recorrer cronologías y abrir evidencias
con los nuevos controles del visor.

La finalización técnica no equivale a una métrica de exactitud. Los campos
derivados se presentan para facilitar la consulta y siempre deben poder
contrastarse con la página del PDF oficial.
