# Actualización a la versión 0.7.2 — consulta documental

Esta versión retira el módulo de evaluación de búsquedas y su banco de casos.
La aplicación se concentra en consultar y analizar las actas oficiales. Los
campos estructurados son ayudas de navegación; el texto y la página del PDF son
siempre la fuente de trabajo.

Se conservan controles automáticos de integridad para impedir que una base
dañada, incompleta o con paquetes inválidos reemplace el índice publicado.
Estos controles no califican ni corrigen el contenido de las actas.

## 1. Subir la actualización

1. Descomprime el ZIP.
2. En el repositorio privado abre **Code → Add file → Upload files**.
3. Arrastra **el contenido** de la carpeta descomprimida, no la carpeta
   exterior.
4. Confirma el commit en `main`.

GitHub no elimina archivos antiguos cuando se sube un ZIP por el navegador.
Por eso esta entrega incluye una acción de limpieza de una sola ejecución.

## 2. Retirar los archivos antiguos

1. Abre **Actions → Retirar módulo de evaluación**.
2. Pulsa **Run workflow → Run workflow**.
3. Espera a que termine en verde.

La acción elimina de forma explícita:

- `pages/6_Evaluacion.py`;
- `services/evaluation.py`;
- `evaluate_search.py`;
- `evaluation_cases.csv`;
- las dos pruebas exclusivas de evaluación;
- `data/evaluation-report.json`, si existe.

Los archivos continúan recuperables desde el historial de Git. La primera
ejecución automática de **Pruebas** puede coincidir con los archivos antiguos y
aparecer roja; el resultado válido es el que ejecutes después de esta limpieza.

## 3. Verificar el código

Abre **Actions → Pruebas → Run workflow** y espera a que quede en verde. El flujo ya no debe ejecutar
`evaluate_search.py`, generar `evaluation-report.json` ni pedir casos de prueba
humanos.

Permanecen activos:

- cobertura de documentos y páginas;
- integridad SQLite, claves internas y FTS;
- texto fuente por página y OCR;
- vigencia del índice semántico;
- hashes, restauración y smoke test de los paquetes;
- trazabilidad al PDF oficial;
- advertencias informativas sobre campos estructurados.

## 4. Publicar el reprocesamiento

El cambio en `reprocess_corpus.py` modifica la huella del proceso. No reutilices
el checkpoint `33891988458` ni otro `resume_run_id` creado con la versión 0.7.1.
La caché de PDF seguirá evitando normalmente las descargas repetidas.

1. Abre **Actions → Reprocesar estructura y fichas**.
2. Ejecuta primero:
   - `mode`: `diagnostic`;
   - `resume_run_id`: vacío;
   - `max_batches`: `0` para intentar terminar en una sola ejecución, o `4`
     para trabajar por lotes.
3. Si usaste `4` y aparece **Reprocesamiento parcial**, vuelve a ejecutar
   pegando como `resume_run_id` el número de la ejecución inmediatamente
   anterior. Repite hasta que finalice la candidata.
4. Comprueba que el resumen indique `diagnostic_complete` y cero bloqueos
   técnicos.
5. Ejecuta otra vez con:
   - `mode`: `publish`;
   - `resume_run_id`: número del diagnóstico completo;
   - `max_batches`: cualquier valor, porque la candidata ya está completa.

Las variaciones de producto, principio activo, interesado, expediente,
radicado, concepto o resultado se muestran como advertencias. No bloquean la
publicación porque son campos derivados. Sí bloquean la publicación la pérdida
de documentos o páginas, una base dañada, errores de extracción no controlados,
identidades técnicas ambiguas, un índice semántico obsoleto o paquetes que no
puedan restaurarse.

## 5. Comprobar Streamlit

1. Espera el redespliegue automático; si no ocurre, pulsa **Reboot app**.
2. En Inicio confirma la versión `0.7.2`.
3. Verifica que ya no aparezca la página **Evaluación**.
4. En **Integridad**, confirma el número esperado de documentos, páginas e
   integridad SQLite correcta.
5. Realiza búsquedas textual, híbrida y semántica.
6. Abre varias evidencias y verifica el documento y la página oficial.
7. Prueba la comparación y la cronología.

No necesitas crear un banco humano ni introducir referencias esperadas.

## Operación posterior

La actualización normal continúa automatizada: **Actualizar catálogo desde
INVIMA** detecta publicaciones y dispara **Construir índice** cuando encuentra
actas nuevas o documentos pendientes.

`LLM_PROVIDER = "prompt_only"` puede mantenerse. No afecta el buscador textual,
híbrido ni semántico local.
