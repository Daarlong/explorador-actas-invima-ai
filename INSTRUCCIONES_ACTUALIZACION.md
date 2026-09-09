# Actualización a la versión 0.9.1

La versión 0.9.1 corrige la construcción inicial del índice neuronal cuando el
corpus no cabe dentro de una sola ejecución de GitHub Actions. La aplicación y
la interfaz de la 0.9.0 se conservan: esta entrega cambia la forma de construir,
reanudar y publicar `semantic.db`.

La entrega es un **overlay de código**. No incluye ni reemplaza
`documents_manifest.csv`, `actas_catalog.csv` ni los archivos publicados dentro
de `data/`.

## 1. Qué cambia

- **Un solo inicio manual:** pulsa **Run workflow** una vez. Si hacen falta más
  segmentos, el workflow los programa automáticamente.
- **Avance durable:** cada segmento procesa como máximo 7.200 segundos o 50.000
  textos únicos y confirma los vectores en `semantic.checkpoint.db`.
- **Lotes mayores:** el modelo trabaja en lotes de 128 textos.
- **Publicación atómica:** la base semántica publicada permanece intacta hasta
  que la candidata alcance el 100 % y supere las verificaciones.
- **Reutilización:** los embeddings se identifican por SHA-256 del texto exacto.
  Las actualizaciones futuras calculan solamente textos nuevos o modificados.
- **Sin operación por identificadores:** no debes pegar `resume_run_id`,
  descargar checkpoints ni subir partes de las bases manualmente.

## 2. Subir la actualización

1. Descomprime el ZIP de la versión 0.9.1.
2. En el repositorio privado abre **Code → Add file → Upload files**.
3. Arrastra **el contenido** de la carpeta descomprimida, no la carpeta
   exterior.
4. Confirma el commit en la rama `main`.

No borres ni reemplaces `data/`, `documents_manifest.csv` o
`actas_catalog.csv`. Tampoco elimines las partes publicadas de `actas.db` o
`semantic.db`.

La 0.9.1 incluye acumulativamente el código de la 0.8.0 y la interfaz de la
0.9.0. No necesitas instalar esas entregas por separado.

## 3. Verificar el código

Después del commit, **Actions → Pruebas** debe iniciarse automáticamente. Espera
a que termine en verde. Si no se inicia, abre **Actions → Pruebas → Run
workflow**, conserva la rama `main` y ejecútalo una vez.

No inicies la construcción del índice mientras las pruebas estén rojas.

## 4. Iniciar la construcción una sola vez

Después de que **Pruebas** quede en verde:

1. abre **Actions → Construir índice**;
2. pulsa **Run workflow**;
3. conserva la rama `main`;
4. confirma **Run workflow** una sola vez.

No vuelvas a pulsar el botón cuando termine el primer segmento. Si todavía hay
embeddings pendientes, esa ejecución guarda el checkpoint y deja la siguiente
en cola automáticamente. La cadena admite hasta 20 segmentos como protección
contra un ciclo sin avance.

## 5. Cómo seguir el progreso

Es normal ver varias ejecuciones consecutivas llamadas **Construir índice**.
Cada una puede quedar en verde y la siguiente aparecer en cola o en curso. Un
segmento verde confirma que su trabajo se guardó; no significa por sí solo que
el índice final ya esté publicado.

Para comprobar el estado:

1. abre la ejecución más reciente de **Construir índice**;
2. entra en **Summary**;
3. busca la sección **Avance del índice neuronal**;
4. revisa estos valores:
   - **Estado:** `en construcción` o `completo`;
   - **Embeddings disponibles:** cantidad ya confirmada;
   - **Pendientes:** cantidad que falta;
   - **Segmentos terminados:** continuaciones completadas.

En el registro de **Construir o actualizar base de búsqueda** también aparece:

```text
Embeddings neuronales confirmados: X/Y
```

La cadena terminó únicamente cuando el resumen indique **Estado: completo**,
**Pendientes: 0** y la ejecución cree el commit automático de las bases
empaquetadas.

`data/semantic-progress.json` contiene esos contadores. Durante los segmentos
intermedios se conserva junto con `data/semantic.checkpoint.db` en Actions
Cache, por lo que no tienes que esperar que cambie en la pestaña **Code** del
repositorio. El reporte se incorpora al repositorio con la publicación final.

No borres las cachés de GitHub Actions mientras la cadena esté en curso. Puedes
cerrar la pestaña del navegador: las ejecuciones continúan en GitHub.

## 6. Qué ocurre con la aplicación durante la construcción

Los segmentos incompletos no hacen commit de `actas.db` ni de `semantic.db`.
Streamlit continúa utilizando las bases que ya estaban publicadas y no necesita
reiniciarse en cada segmento.

Cuando la cobertura neuronal llega al 100 %, el workflow:

1. valida la integridad SQLite, la dimensión, el modelo y la huella del corpus;
2. promueve atómicamente `semantic.checkpoint.db` a `semantic.db`;
3. comprime y verifica `actas.db` y `semantic.db`;
4. divide los paquetes en partes de hasta 90 MiB;
5. crea el commit automático en `main`.

Ese commit activa el redespliegue de Streamlit. Espera unos minutos. Si la
aplicación sigue mostrando la versión anterior después de finalizar el
despliegue, abre el panel de Streamlit y pulsa **Reboot app** una sola vez.

## 7. Tiempo y consumo de GitHub Actions

La primera construcción neuronal del histórico sigue siendo un trabajo grande.
La 0.9.1 evita perder el progreso, pero no elimina el cálculo: puede requerir
varias ejecuciones y consumir una cantidad importante de minutos de GitHub
Actions. Cada segmento neuronal puede usar hasta dos horas, además del tiempo
de preparación y verificación.

No es necesario mantener el navegador abierto. Revisa el saldo o la política de
minutos de Actions de la organización antes de iniciar la primera carga si el
repositorio tiene un límite estricto.

Después de completar esa primera base, las actualizaciones normales son mucho
más pequeñas. Los textos que mantienen el mismo SHA-256 reutilizan su embedding;
una nueva acta suele requerir inferencia únicamente para sus fragmentos nuevos o
modificados.

## 8. Si una ejecución se detiene o queda roja

El último segmento guardado correctamente permanece en Actions Cache. Inicia
**Construir índice** una vez más; el workflow intentará restaurar ese checkpoint
y continuar sin repetir los lotes confirmados.

Detén la operación y revisa los registros si ocurre alguno de estos casos:

- el resumen informa que no hubo progreso;
- se alcanza el límite de 20 segmentos;
- falla la validación de integridad;
- la ejecución vuelve a fallar en el mismo lote.

No soluciones esos casos borrando las bases publicadas, editando
`semantic-progress.json` o vaciando las cachés. Esas acciones eliminarían la
posibilidad de continuar desde el último punto válido.

## 9. Comprobación final en Streamlit

Después del commit y del redespliegue:

1. **Inicio** debe mostrar la versión `0.9.1`.
2. El corpus debe aparecer disponible.
3. En **Explorador**, prueba una consulta en modo textual, híbrido y semántico.
4. Confirma que los resultados abran el PDF y la página correctos.
5. Revisa **Integridad** y verifica que no haya documentos pendientes o errores
   de SQLite.

## Configuración

Mantén:

```toml
LLM_PROVIDER = "prompt_only"
```

Ese valor evita llamadas generativas y no desactiva los embeddings neuronales,
que se construyen localmente dentro de GitHub Actions. Si usas la página de
Administración, conserva una `ADMIN_PASSWORD` larga y privada en los secretos de
Streamlit; nunca la subas a GitHub.

## Operación posterior

**Actualizar catálogo desde INVIMA** continúa revisando la fuente oficial de
lunes a viernes. Cuando detecta una nueva publicación o un documento pendiente,
inicia **Construir índice**. Si la actualización requiere más de un segmento,
las continuaciones también se programan automáticamente.
