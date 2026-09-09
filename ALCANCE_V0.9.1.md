# Alcance cerrado de la versión 0.9.1

La versión 0.9.1 corrige la construcción y actualización del índice semántico
neuronal para que un corpus grande pueda completarse en varias ejecuciones de
GitHub Actions sin perder el trabajo confirmado. No cambia la experiencia de
consulta introducida en la 0.9.0 ni incorpora IA generativa.

## Imprescindible

1. **Checkpoint neuronal durable**
   - Construir la candidata en `data/semantic.checkpoint.db`, separada de
     `data/semantic.db`.
   - Confirmar cada lote de embeddings antes de continuar con el siguiente.
   - Restaurar desde Actions Cache el último checkpoint compatible cuando una
     ejecución posterior continúe la cadena.
   - Validar que el checkpoint corresponda al mismo corpus, modelo, revisión,
     dimensiones, codificación y firma de entrada antes de reutilizarlo.

2. **Identidad estable y deduplicación**
   - Identificar cada texto neuronal mediante SHA-256 de sus bytes UTF-8
     exactos, sin depender exclusivamente del `item_id` de SQLite.
   - Calcular una sola vez el embedding de textos idénticos y asociarlo con
     todos los fragmentos correspondientes.
   - Reutilizar vectores compatibles aunque una reconstrucción cambie los
     identificadores internos de los fragmentos.

3. **Construcción segmentada y continuación automática**
   - Limitar cada segmento a 7.200 segundos o 50.000 textos únicos.
   - Procesar lotes de 128 textos y conservar el avance confirmado al terminar
     cada segmento.
   - Programar automáticamente el siguiente `workflow_dispatch` mientras haya
     embeddings pendientes.
   - Detener la cadena si no hubo progreso o después de 20 segmentos, evitando
     ciclos indefinidos.
   - Requerir un solo inicio manual de **Construir índice** para una cadena
     normal.

4. **Publicación atómica y sin regresión del servicio**
   - Mantener intactas las bases empaquetadas que usa Streamlit durante todos
     los segmentos incompletos.
   - No hacer commit de una candidata parcial ni presentarla como disponible.
   - Exigir cobertura neuronal del 100 %, integridad SQLite, dimensiones
     válidas, modelo correcto y huella coincidente antes de publicar.
   - Promover el checkpoint completo a `semantic.db` mediante reemplazo
     atómico y solo entonces empaquetar, verificar y hacer commit.

5. **Progreso observable**
   - Generar `data/semantic-progress.json` con, como mínimo, estado completo,
     avance anterior y actual, cantidad pendiente y segmentos terminados.
   - Mostrar en el resumen de GitHub Actions el estado, los embeddings
     disponibles, los pendientes y los segmentos terminados.
   - Registrar durante la inferencia mensajes del tipo
     `Embeddings neuronales confirmados: X/Y`.
   - Distinguir explícitamente un segmento verde de una cadena completamente
     publicada.

6. **Actualización neuronal incremental**
   - Reutilizar desde el índice publicado los embeddings cuyo SHA-256 siga
     presente y cuya firma neuronal sea compatible.
   - Calcular en ejecuciones futuras únicamente textos nuevos o modificados.
   - Eliminar de la candidata las asociaciones con fragmentos retirados sin
     comprometer los embeddings todavía reutilizables.
   - Recalcular las señales TF-IDF y distribucionales cuando cambie el corpus,
     porque dependen del conjunto documental completo.

7. **Compatibilidad y degradación segura**
   - Conservar la búsqueda textual FTS5 y la semántica determinística mientras
     la candidata neuronal continúa en construcción.
   - Mantener el modo `prompt_only` y no exigir claves de API.
   - Rechazar checkpoints dañados o incompatibles sin sustituir el índice
     publicado correcto.
   - Conservar las pruebas existentes de búsqueda, trazabilidad y ranking, y
     añadir cobertura automatizada para reanudación, reutilización y promoción
     atómica.

8. **Operación documentada**
   - Explicar el flujo de un solo clic y las continuaciones automáticas.
   - Advertir que la primera carga puede requerir varias horas acumuladas y
     consumir una cantidad importante de minutos de GitHub Actions.
   - Explicar cómo reconocer avance, finalización, detención sin progreso y
     publicación final.
   - Indicar que no deben descargarse, editarse ni subirse manualmente el
     checkpoint o el reporte intermedio.

## Deseable, no bloqueante para 0.9.1

- Medir automáticamente vectores por segundo, duración estimada y memoria por
  lote en el resumen de Actions.
- Elegir dinámicamente el tamaño de lote después de una prueba corta del runner.
- Paralelizar la primera carga mediante shards cuando el presupuesto y los
  límites de concurrencia de GitHub lo permitan.
- Incorporar una vista de progreso de mantenimiento dentro de la aplicación sin
  exponer archivos temporales.
- Adoptar en el futuro un índice vectorial ANN si la búsqueda semántica global
  requiere menor latencia.

## Fuera de alcance

- Reducir el corpus neuronal a fichas, páginas completas o actas seleccionadas;
  se conserva la cobertura de los fragmentos consultables.
- Cambiar el tamaño o el solapamiento de los fragmentos existentes.
- Cambiar el modelo multilingüe, los pesos de ranking o los modos textual,
  híbrido y semántico.
- Añadir consultas generativas, RAG, prompts nuevos o integración con una API de
  IA; `LLM_PROVIDER = "prompt_only"` continúa siendo la configuración indicada.
- Cambiar la interfaz de la 0.9.0, reemplazar Streamlit o incorporar un frontend
  independiente.
- Añadir módulos de evaluación, calificación o corrección de las actas.
- Migrar el almacenamiento a PostgreSQL, un servicio vectorial administrado o
  infraestructura con GPU.
- Prometer que la segmentación reduce el cálculo o los minutos totales de la
  primera carga; su objetivo es conservar y continuar el avance.

## Criterio de finalización

La versión queda lista cuando:

- una ejecución interrumpida después de uno o más lotes puede continuar sin
  recalcular los embeddings ya confirmados;
- textos idénticos comparten un solo embedding y un corpus actualizado reutiliza
  todos los vectores compatibles;
- cada segmento incompleto guarda un checkpoint íntegro y programa el siguiente
  sin intervención del usuario;
- una candidata parcial nunca modifica los paquetes publicados;
- el último segmento solo publica después de alcanzar cero pendientes y superar
  todas las validaciones;
- `semantic-progress.json` y el resumen de Actions permiten diferenciar con
  claridad construcción, continuación y finalización;
- las pruebas automatizadas pasan y las búsquedas textual, híbrida y semántica
  continúan devolviendo evidencia trazable al PDF y a la página oficial.
