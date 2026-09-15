# Actualización a la versión 0.11.0

La 0.11.0 incorpora validación técnica sobre el corpus publicado, expansión
segura con un diccionario regulatorio local, facetas con conteos globales y un
tablero analítico descriptivo. No incorpora IA generativa ni un módulo de
evaluación o corrección de actas.

## Actualizar desde la versión 0.10.0

1. Conserva `data/`, `documents_manifest.csv` y `actas_catalog.csv`.
2. Descomprime `explorador-actas-invima-ai-v0.11.0-overlay.zip`.
3. En el repositorio privado abre **Code → Add file → Upload files** y arrastra
   el contenido de la carpeta descomprimida, no la carpeta exterior.
4. Confirma el commit en `main` y espera que **Actions → Pruebas** quede verde.
5. Espera el redespliegue de Streamlit y confirma la versión `v0.11.0`.
6. Abre **Actions → Validar búsquedas publicadas → Run workflow**. Revisa que
   los contratos técnicos terminen correctamente; las sondas de relevancia son
   informativas y no bloquean la publicación.
7. Comprueba una consulta con sinónimos, los conteos de las facetas y la
   navegación desde el tablero hasta un PDF oficial.

Esta actualización utiliza las tres bases ya publicadas por la 0.10.0. No
ejecutes **Construir índice** únicamente por instalarla y no recalcules los
embeddings: el diccionario se aplica al consultar y las agregaciones leen el
esquema existente. El workflow de construcción seguirá ejecutándose de forma
automática cuando el catálogo detecte una acta nueva o un documento pendiente.

Mantén esta configuración:

```toml
LLM_PROVIDER = "prompt_only"
```

El alcance cerrado está en `ALCANCE_V0.11.md`. Las instrucciones siguientes se
conservan para instalaciones que todavía deban preparar los índices de la
versión 0.10.0.

---

## Preparación de los índices de la versión 0.10.0

Esta versión añade recuperación híbrida global, índice ANN local, búsqueda por
campo, frases sobre la página completa, transparencia del motor, paginación en
la capa de datos y exportación CSV/XLSX.

## Antes de empezar

- Conserva todos los archivos actuales dentro de `data/`.
- No reemplaces `documents_manifest.csv` ni `actas_catalog.csv` con archivos
  vacíos o antiguos.
- Mantén `LLM_PROVIDER = "prompt_only"`; esta versión no necesita una clave de
  IA generativa.
- No inicies más de una construcción manual. Si hacen falta segmentos para
  completar embeddings, el workflow los programa por sí mismo.

## Instalación

1. Descomprime `explorador-actas-invima-ai-v0.10.0-overlay.zip`.
2. En el repositorio privado abre **Code → Add file → Upload files**.
3. Arrastra **el contenido de la carpeta descomprimida**, no la carpeta exterior.
4. Confirma el commit en `main`.
5. Espera que **Actions → Pruebas** termine en verde.
6. Abre **Actions → Construir índice → Run workflow** y ejecútalo una sola vez
   desde `main`.
7. Espera a que el resumen indique embeddings completos y a que el workflow
   cree el commit automático de las bases empaquetadas.
8. Comprueba en `data/` la presencia de estos manifiestos y sus partes:

   - `actas.db.package.json`
   - `semantic.db.package.json`
   - `semantic-ann.db.package.json`

9. Espera el redespliegue de Streamlit y confirma que la portada muestre
   `v0.10.0`. Si no cambia después de unos minutos, usa **Reboot app** una vez.

## Qué hace la construcción

El workflow migra `actas.db` al esquema 7, actualiza el corpus, reutiliza los
embeddings vigentes y completa únicamente los que falten. Solo cuando el índice
neuronal está completo ejecuta:

```text
python build_ann.py --semantic data/semantic.db --output data/semantic-ann.db
```

Después comprime, divide, restaura y verifica las tres bases antes de hacer el
commit. `semantic-ann.db` se deriva de `semantic.db`; construirlo no vuelve a
analizar los PDF ni recalcula sus embeddings.

## Comportamiento durante el despliegue

Es seguro que Streamlit se redespliegue primero con el código nuevo. Mientras no
estén publicados el esquema 7 o `semantic-ann.db`, la aplicación debe:

- seguir permitiendo las búsquedas compatibles con la base anterior;
- usar la ruta semántica de respaldo cuando corresponda;
- identificar el motor realmente utilizado;
- ocultar o desactivar solamente las opciones que dependan del componente que
  falta.

La ausencia temporal del ANN no implica que debas borrar bases, subir archivos
manualmente ni volver a iniciar el workflow.

## Prueba funcional mínima

Cuando termine el commit automático:

1. busca una frase conocida que atraviese dos fragmentos y activa **Exigir frase
   completa**;
2. repite una consulta en modo **Híbrido** y confirma que la aplicación indique
   el uso real del motor neuronal/ANN;
3. limita una consulta a **Principio activo** o **Solicitud/indicación**;
4. avanza a otra página de resultados y confirma que no se repitan actas;
5. descarga CSV y XLSX y verifica acta, página, fragmento y URL oficial.

## Actualizaciones posteriores

La inclusión automática de nuevas actas sigue activa. **Actualizar catálogo
desde INVIMA** consulta la fuente oficial de lunes a viernes y, cuando detecta
una publicación o documento pendiente, inicia **Construir índice**. La ejecución
incremental reutiliza los embeddings existentes y genera otra vez el ANN con el
corpus actualizado; no requiere intervención manual en condiciones normales.

El alcance y las exclusiones definitivas están en `ALCANCE_V0.10.md`.
