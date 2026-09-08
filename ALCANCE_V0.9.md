# Alcance cerrado de la versión 0.9.0

La versión 0.9.0 mejora la navegación y la experiencia de consulta sin cambiar
el corpus ni las capacidades documentales de la 0.8.0. El PDF oficial y la
página citada continúan siendo la fuente de referencia.

## Imprescindible

1. **Sistema visual compartido**
   - Aplicar tipografía, espaciado, contenedores, botones, métricas, estados y
     enlaces consistentes en todas las páginas.
   - Definir temas claro y oscuro desde la configuración nativa de Streamlit.
   - Mantener contraste, foco visible, etiquetas textuales para los estados y
     reducción de movimiento cuando el sistema lo solicite.
   - Adaptar el contenido a escritorio y pantallas estrechas sin depender de
     fuentes, imágenes o recursos externos.

2. **Navegación superior orientada por tareas**
   - Mostrar directamente **Inicio**, **Explorar**, **Comparar** y
     **Analizar fuentes**.
   - Agrupar **Catálogo** e **Integridad** como recursos del corpus y
     **Administración** como función de gestión.
   - Registrar todos los destinos internos en una sola navegación para que los
     enlaces entre páginas funcionen de forma predecible.

3. **Portada simplificada**
   - Dar prioridad a una búsqueda que transfiera la consulta al Explorador.
   - Ofrecer accesos claros para explorar actas, comparar precedentes y
     preparar un análisis de fuentes.
   - Mostrar el estado del corpus de forma compacta y dejar cobertura,
     actividad e indicadores técnicos en una sección desplegable.

4. **Explorador en vista lista–detalle**
   - Organizar filtros por grupos, informar cuántos están activos y permitir
     restablecerlos.
   - Mantener los modos textual, híbrido y semántico, la paginación y el orden
     de resultados existentes.
   - Presentar cada resultado con metadatos, fragmento, coincidencias y acceso
     a la fuente; conservar una evidencia activa en el panel de detalle.
   - Permitir seleccionar evidencias y enviarlas a Comparar o a Analizar
     fuentes sin perder el contexto de la consulta.
   - Ofrecer instrucciones útiles cuando no hay consulta, resultados o índice.

5. **Visor documental integrado**
   - Mantener la evidencia visible junto a la lista cuando el ancho lo permita.
   - Conservar salto de página, anterior/siguiente, zoom, rotación, búsqueda,
     copia o descarga del texto y apertura del PDF oficial.
   - Identificar claramente documento, página y disponibilidad del texto.

6. **Comparación y cronología más legibles**
   - Guiar la búsqueda, selección y comparación sin exigir una selección previa
     desde otra página.
   - Separar diferencias, matriz y fuentes, con enlaces inequívocos a la
     evidencia correspondiente.
   - Mantener la cronología por campos, filtros, paginación, vista visual,
     detalle, tabla y descargas existentes.
   - Representar resultados favorables, no favorables y requerimientos con
     texto y tratamiento visual coherentes.

7. **Consistencia de páginas secundarias**
   - Aplicar el sistema visual y una jerarquía de información común a
     **Analizar fuentes**, **Catálogo**, **Integridad** y **Administración**.
   - Explicar de forma visible que `prompt_only` prepara fuentes sin enviar
     información a una IA generativa.
   - Mantener el acceso restringido y las advertencias de persistencia en
     Administración.

8. **Enfoque exclusivamente documental**
   - Retirar de la interfaz referencias visibles a evaluación, calificación o
     corrección de las actas y de los campos extraídos.
   - Mantener la trazabilidad al texto y a la página oficial como mecanismo de
     consulta, sin presentar un flujo de revisión humana.

9. **Compatibilidad funcional y de datos**
   - Conservar el catálogo, el manifiesto, los índices, la búsqueda, la
     selección, las descargas y el estado de sesión existentes.
   - Distribuir la entrega como overlay de código sin incluir ni sobrescribir
     `documents_manifest.csv`, `actas_catalog.csv` o `data/`.
   - No exigir una reconstrucción del índice cuando la base 0.8.0 ya está
     publicada.

## Deseable, no bloqueante para 0.9.0

- Añadir regresión visual automatizada con capturas en tema claro y oscuro.
- Guardar búsquedas, filtros o comparaciones favoritas por usuario.
- Incorporar identidad gráfica corporativa cuando exista una guía y activos
  aprobados.
- Refinar layouts específicos para teléfonos después de probarlos con usuarios
  reales y con el corpus desplegado.
- Centralizar analítica de uso respetando las políticas corporativas y sin
  registrar consultas sensibles.

## Fuera de alcance

- Cambiar la extracción, el catálogo, los manifiestos, el contenido de las
  bases o el ordenamiento de búsqueda.
- Construir o reprocesar el índice únicamente por instalar esta versión visual.
- Activar, ampliar o reconfigurar la compatibilidad existente con IA generativa,
  RAG, nuevos prompts o un proveedor LLM; la operación recomendada continúa en
  `prompt_only`.
- Módulo de evaluación, calificación, banco de casos o corrección manual.
- Cambiar autenticación, SSO, infraestructura o almacenamiento a una plataforma
  corporativa nueva.
- Reemplazar Streamlit por React, Next.js o un componente JavaScript propio.

## Criterio de finalización

La versión queda lista cuando:

- las pruebas automatizadas y la compilación pasan;
- todos los destinos internos se abren sin errores de Streamlit;
- Inicio, Explorador, visor, Comparar, cronología y páginas secundarias conservan
  sus operaciones anteriores;
- una comprobación manual valida temas claro y oscuro y anchos de escritorio,
  tableta y móvil;
- no quedan referencias visibles a evaluación o corrección;
- el despliegue sobre una base 0.8.0 operativa no reconstruye ni modifica el
  corpus.
