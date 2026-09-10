# Alcance cerrado de la versión 0.9.2

## Objetivo

Corregir la búsqueda de frases sobre la base SQLite ya publicada, sin cambiar
el esquema, reconstruir `actas.db` ni recalcular los 321.246 embeddings.

## Incluido

1. La opción **Exigir frase completa** deja de enviar a FTS5 una consulta de
   frase incompatible con el esquema compacto `detail=column`.
2. La preselección usa términos atómicos unidos por `AND` y confirma la
   secuencia literal antes de aplicar los límites de resultados y por acta.
3. La comparación de frase ignora mayúsculas, tildes, puntuación y diferencias
   de espacios o saltos de línea, pero conserva el orden de las palabras.
4. Las búsquedas textual e híbrida ejecutan un carril literal adicional para
   que una cita conocida no se pierda detrás de coincidencias dispersas.
5. En modo híbrido, una coincidencia literal se conserva durante la fusión y
   tiene prioridad sobre una paráfrasis que el modelo neuronal puntúe mejor.
6. Consultas con separadores como `beneficio-riesgo`, `beneficio_riesgo` o
   `beneficio/riesgo` se convierten en términos FTS atómicos y no generan el
   error de frases no soportadas.
7. Se incorporan pruebas de regresión para el esquema real `detail=column`,
   normalización, orden de palabras, truncado por documento y ranking híbrido.

## No incluido

- Cambios en `actas.db`, `semantic.db`, catálogos o manifiestos.
- Reprocesamiento de PDF, OCR o embeddings.
- Búsqueda garantizada de frases de más de 180 caracteres que crucen exactamente
  el límite entre dos fragmentos. Esa ampliación requeriría un índice textual
  adicional por página o un escaneo más costoso del texto fuente.
- Consulta generativa mediante una API de IA.

## Criterios de aceptación

- Una frase literal de varias palabras se recupera con **Exigir frase completa**
  sin producir `fts5: phrase queries are not supported (detail!=full)`.
- Una frase literal queda por encima de resultados con las mismas palabras en
  otro orden en los modos textual e híbrido.
- El comportamiento funciona sobre una base creada con `detail=column`.
- Las pruebas automatizadas terminan en verde.
- La instalación solo exige subir el overlay y esperar el redespliegue de
  Streamlit; no se ejecuta **Construir índice**.
