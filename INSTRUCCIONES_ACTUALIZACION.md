# Actualización a la versión 0.9.2

Esta corrección soluciona el error:

```text
fts5: phrase queries are not supported (detail!=full)
```

También evita que una frase literal conocida quede fuera de los primeros
resultados en los modos textual e híbrido.

## Qué debes hacer

1. Descomprime el ZIP `explorador-actas-invima-ai-v0.9.2-overlay.zip`.
2. En el repositorio privado abre **Code → Add file → Upload files**.
3. Arrastra **el contenido de la carpeta descomprimida**, no la carpeta exterior.
4. Confirma el commit en `main`.
5. Espera que **Actions → Pruebas** termine en verde.
6. Espera el redespliegue automático de Streamlit y comprueba que la portada
   muestre `v0.9.2`. Si no cambia después de unos minutos, pulsa **Reboot app**
   una sola vez desde Streamlit.

## Qué no debes hacer

- No ejecutes **Construir índice** por esta actualización.
- No reemplaces ni borres nada dentro de `data/`.
- No vuelvas a calcular los embeddings.
- No reemplaces `documents_manifest.csv` ni `actas_catalog.csv`.

El overlay excluye deliberadamente esas bases y archivos de catálogo, por lo
que al subirlo no debería ofrecerte sustituirlos.

## Prueba funcional

En **Explorar**:

1. selecciona modo **Híbrido**;
2. pega una frase literal que conozcas de un acta;
3. confirma que la coincidencia literal aparezca antes que las coincidencias
   aproximadas;
4. activa **Exigir frase completa** y repite la consulta;
5. confirma que ya no aparezca el error de FTS5 y que solo se muestren
   fragmentos con las palabras consecutivas y en el mismo orden.

La comparación tolera diferencias de mayúsculas, tildes, puntuación y saltos
de línea. Si una frase concreta sigue sin aparecer, registra la frase exacta,
el acta y la página: eso permitiría determinar si el texto no fue extraído del
PDF o si cruza el límite excepcional entre dos fragmentos.

## Configuración

Mantén sin cambios:

```toml
LLM_PROVIDER = "prompt_only"
```

La corrección no utiliza una API externa ni modifica la automatización que
incorpora nuevas actas.
