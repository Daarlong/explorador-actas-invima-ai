"""Exportaciones tabulares seguras para resultados de consulta.

El modulo no conoce Streamlit ni ejecuta busquedas. Recibe filas ya
recuperadas (o instancias de :class:`~services.models.SearchResult`) para que
la capa de aplicacion pueda exportar una pagina o, preferiblemente, recorrer
el conjunto completo mediante un iterador paginado.

Las funciones nunca truncan una exportacion silenciosamente. Si se supera un
limite de filas, bytes, columnas o longitud de celda, se descarta el resultado
y se informa mediante :class:`ExportLimitError`.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from io import BytesIO, StringIO
from itertools import islice
import math
import re
from typing import Iterable, Mapping, Sequence

from services.models import SearchResult


CSV_MIME_TYPE = "text/csv; charset=utf-8"
XLSX_MIME_TYPE = (
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)
DEFAULT_MAX_CSV_ROWS = 50_000
DEFAULT_MAX_XLSX_ROWS = 20_000
DEFAULT_MAX_EXPORT_BYTES = 25 * 1024 * 1024
MAX_EXPORT_COLUMNS = 256
MAX_EXCEL_CELL_CHARACTERS = 32_767
EXCEL_MAX_DATA_ROWS = 1_048_575  # La primera fila se reserva para encabezados.


SEARCH_EXPORT_COLUMNS: tuple[tuple[str, str], ...] = (
    ("position", "posicion"),
    ("document_id", "id_documento"),
    ("chunk_id", "id_fragmento"),
    ("title", "documento"),
    ("year", "ano"),
    ("acta_number", "acta"),
    ("section", "sala_seccion"),
    ("part", "parte"),
    ("page", "pagina"),
    ("text", "fragmento"),
    ("match_excerpt", "evidencia_coincidente"),
    ("evidence_scope", "unidad_evidencia"),
    ("matched_field", "campo_coincidente"),
    ("match_type", "tipo_coincidencia"),
    ("score", "puntaje"),
    ("lexical_score", "puntaje_textual"),
    ("semantic_score", "puntaje_semantico"),
    ("source_type", "tipo_fuente"),
    ("url", "fuente"),
)

SEARCH_EXPORT_METADATA: tuple[tuple[str, str], ...] = (
    ("consulta", "consulta"),
    ("metodo_solicitado", "metodo_solicitado"),
    ("motor_real", "motor_real"),
    ("campo", "campo_consultado"),
    ("filtros", "filtros"),
    ("frase_completa", "frase_completa"),
    ("ruta_respaldo", "ruta_respaldo"),
    ("totales_exactos", "totales_exactos"),
)


class ExportLimitError(ValueError):
    """La exportacion solicitada excede un limite de seguridad explicito."""


class ExportUnavailableError(RuntimeError):
    """El formato solicitado no esta disponible en la instalacion actual."""


@dataclass(frozen=True)
class ExportArtifact:
    """Archivo listo para entregarse mediante ``st.download_button``."""

    data: bytes
    file_name: str
    mime_type: str
    row_count: int
    file_format: str


def _validate_limits(
    *,
    headers: Sequence[object],
    max_rows: int,
    max_bytes: int,
) -> None:
    if not headers:
        raise ValueError("La exportacion necesita al menos una columna")
    if len(headers) > MAX_EXPORT_COLUMNS:
        raise ExportLimitError(
            f"La exportacion supera el limite de {MAX_EXPORT_COLUMNS} columnas"
        )
    if max_rows < 1:
        raise ValueError("max_rows debe ser mayor que cero")
    if max_bytes < 1:
        raise ValueError("max_bytes debe ser mayor que cero")


def _safe_text(value: object) -> str:
    """Convierte texto documental sin permitir formulas de hoja de calculo."""

    if value is None:
        return ""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    text = str(value).replace("\x00", "")
    if text.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def _safe_cell(value: object) -> object:
    """Conserva numeros reales y trata cualquier texto como dato literal."""

    if value is None:
        return ""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else _safe_text(value)
    return _safe_text(value)


def _validated_row(
    row: Sequence[object],
    *,
    width: int,
    row_number: int,
    enforce_excel_cell_limit: bool,
) -> list[object]:
    values = list(row)
    if len(values) != width:
        raise ValueError(
            f"La fila {row_number} tiene {len(values)} columnas; se esperaban {width}"
        )
    safe = [_safe_cell(value) for value in values]
    if enforce_excel_cell_limit:
        for column_number, value in enumerate(safe, start=1):
            if isinstance(value, str) and len(value) > MAX_EXCEL_CELL_CHARACTERS:
                raise ExportLimitError(
                    "La celda de la fila "
                    f"{row_number}, columna {column_number}, supera el limite "
                    f"de Excel de {MAX_EXCEL_CELL_CHARACTERS} caracteres"
                )
    return safe


def rows_to_csv(
    headers: Sequence[object],
    rows: Iterable[Sequence[object]],
    *,
    max_rows: int = DEFAULT_MAX_CSV_ROWS,
    max_bytes: int = DEFAULT_MAX_EXPORT_BYTES,
) -> bytes:
    """Serializa filas en CSV UTF-8 con BOM y limites no silenciosos."""

    _validate_limits(headers=headers, max_rows=max_rows, max_bytes=max_bytes)
    output = bytearray(b"\xef\xbb\xbf")

    def append_row(row: Sequence[object], row_number: int) -> None:
        values = _validated_row(
            row,
            width=len(headers),
            row_number=row_number,
            enforce_excel_cell_limit=False,
        )
        buffer = StringIO(newline="")
        csv.writer(buffer, lineterminator="\n").writerow(values)
        encoded = buffer.getvalue().encode("utf-8")
        if len(output) + len(encoded) > max_bytes:
            raise ExportLimitError(
                "La exportacion CSV supera el limite de "
                f"{max_bytes / (1024 * 1024):.1f} MiB; acota la consulta"
            )
        output.extend(encoded)

    append_row(list(headers), 1)
    for data_position, row in enumerate(rows, start=1):
        if data_position > max_rows:
            raise ExportLimitError(
                "La exportacion CSV supera el limite de "
                f"{max_rows} filas; acota la consulta"
            )
        append_row(row, data_position + 1)
    return bytes(output)


def _safe_sheet_name(value: str) -> str:
    name = re.sub(r"[\[\]:*?/\\]", " ", str(value or "")).strip(" '")
    return (name or "Resultados")[:31]


def _write_xlsx_value(worksheet, row: int, column: int, value: object, cell_format=None):
    """Escribe strings explicitamente para desactivar formulas y autoenlaces."""

    if isinstance(value, bool):
        return worksheet.write_boolean(row, column, value, cell_format)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return worksheet.write_number(row, column, value, cell_format)
    return worksheet.write_string(row, column, str(value), cell_format)


def rows_to_xlsx(
    headers: Sequence[object],
    rows: Iterable[Sequence[object]],
    *,
    sheet_name: str = "Resultados",
    metadata: Mapping[object, object] | None = None,
    max_rows: int = DEFAULT_MAX_XLSX_ROWS,
    max_bytes: int = DEFAULT_MAX_EXPORT_BYTES,
) -> bytes:
    """Crea un XLSX en memoria constante, si XlsxWriter esta disponible."""

    _validate_limits(headers=headers, max_rows=max_rows, max_bytes=max_bytes)
    if max_rows > EXCEL_MAX_DATA_ROWS:
        raise ValueError(
            f"max_rows no puede superar {EXCEL_MAX_DATA_ROWS} para XLSX"
        )
    try:
        import xlsxwriter
    except ImportError as exc:  # pragma: no cover - depende del despliegue.
        raise ExportUnavailableError(
            "La exportacion XLSX no esta disponible. Instala XlsxWriter o usa CSV."
        ) from exc

    output = BytesIO()
    workbook = xlsxwriter.Workbook(
        output,
        {
            "constant_memory": True,
            "in_memory": False,
            "strings_to_formulas": False,
            "strings_to_urls": False,
        },
    )
    closed = False
    try:
        worksheet = workbook.add_worksheet(_safe_sheet_name(sheet_name))
        header_format = workbook.add_format(
            {
                "bold": True,
                "font_color": "#FFFFFF",
                "bg_color": "#1F4E78",
            }
        )
        safe_headers = _validated_row(
            list(headers),
            width=len(headers),
            row_number=1,
            enforce_excel_cell_limit=True,
        )
        column_widths = [min(60, max(10, len(str(value)) + 2)) for value in safe_headers]
        for column, value in enumerate(safe_headers):
            _write_xlsx_value(worksheet, 0, column, value, header_format)

        row_count = 0
        for row_count, row in enumerate(rows, start=1):
            if row_count > max_rows:
                raise ExportLimitError(
                    "La exportacion XLSX supera el limite de "
                    f"{max_rows} filas; acota la consulta"
                )
            values = _validated_row(
                row,
                width=len(headers),
                row_number=row_count + 1,
                enforce_excel_cell_limit=True,
            )
            for column, value in enumerate(values):
                _write_xlsx_value(worksheet, row_count, column, value)
                if row_count <= 200:
                    column_widths[column] = min(
                        60,
                        max(column_widths[column], len(str(value).split("\n", 1)[0]) + 2),
                    )

        worksheet.freeze_panes(1, 0)
        worksheet.autofilter(0, 0, row_count, len(headers) - 1)
        for column, width in enumerate(column_widths):
            worksheet.set_column(column, column, width)

        if metadata:
            metadata_name = (
                "Metadatos" if _safe_sheet_name(sheet_name) == "Consulta" else "Consulta"
            )
            metadata_sheet = workbook.add_worksheet(metadata_name)
            metadata_sheet.write_string(0, 0, "campo", header_format)
            metadata_sheet.write_string(0, 1, "valor", header_format)
            for index, (key, value) in enumerate(metadata.items(), start=1):
                safe_pair = _validated_row(
                    [_safe_text(key), _safe_text(value)],
                    width=2,
                    row_number=index + 1,
                    enforce_excel_cell_limit=True,
                )
                _write_xlsx_value(metadata_sheet, index, 0, safe_pair[0])
                _write_xlsx_value(metadata_sheet, index, 1, safe_pair[1])
            metadata_sheet.freeze_panes(1, 0)
            metadata_sheet.set_column(0, 0, 26)
            metadata_sheet.set_column(1, 1, 70)

        workbook.close()
        closed = True
    except Exception:
        if not closed:
            try:
                workbook.close()
            except Exception:
                pass
        raise

    data = output.getvalue()
    if len(data) > max_bytes:
        raise ExportLimitError(
            "La exportacion XLSX supera el limite de "
            f"{max_bytes / (1024 * 1024):.1f} MiB; acota la consulta"
        )
    return data


def _result_value(result: SearchResult | Mapping[str, object], name: str) -> object:
    if isinstance(result, Mapping):
        return result.get(name)
    return getattr(result, name, None)


def search_result_rows(
    results: Iterable[SearchResult | Mapping[str, object]],
) -> Iterable[tuple[object, ...]]:
    """Transforma resultados en filas, una por fragmento y sin perder scores."""

    keys = [key for key, _ in SEARCH_EXPORT_COLUMNS]
    for position, result in enumerate(results, start=1):
        values = {
            key: _result_value(result, key)
            for key in keys
            if key != "position"
        }
        values["position"] = position
        yield tuple(values.get(key) for key in keys)


def search_results_to_csv(
    results: Iterable[SearchResult | Mapping[str, object]],
    *,
    max_rows: int = DEFAULT_MAX_CSV_ROWS,
    max_bytes: int = DEFAULT_MAX_EXPORT_BYTES,
) -> bytes:
    return rows_to_csv(
        [label for _, label in SEARCH_EXPORT_COLUMNS],
        search_result_rows(results),
        max_rows=max_rows,
        max_bytes=max_bytes,
    )


def search_results_to_xlsx(
    results: Iterable[SearchResult | Mapping[str, object]],
    *,
    metadata: Mapping[object, object] | None = None,
    max_rows: int = DEFAULT_MAX_XLSX_ROWS,
    max_bytes: int = DEFAULT_MAX_EXPORT_BYTES,
) -> bytes:
    return rows_to_xlsx(
        [label for _, label in SEARCH_EXPORT_COLUMNS],
        search_result_rows(results),
        metadata=metadata,
        max_rows=max_rows,
        max_bytes=max_bytes,
    )


def _search_results_with_metadata_to_csv(
    results: Iterable[SearchResult | Mapping[str, object]],
    metadata: Mapping[object, object],
    *,
    max_rows: int,
    max_bytes: int,
) -> bytes:
    """Incluye la consulta en cada fila porque CSV no admite otra hoja."""

    normalized_metadata = {str(key): value for key, value in metadata.items()}
    metadata_columns = [
        (key, label)
        for key, label in SEARCH_EXPORT_METADATA
        if key in normalized_metadata
    ]
    headers = [label for _, label in metadata_columns] + [
        label for _, label in SEARCH_EXPORT_COLUMNS
    ]

    def rows():
        metadata_values = [normalized_metadata[key] for key, _ in metadata_columns]
        for result_values in search_result_rows(results):
            yield (*metadata_values, *result_values)

    return rows_to_csv(
        headers,
        rows(),
        max_rows=max_rows,
        max_bytes=max_bytes,
    )


def export_search_results(
    results: Iterable[SearchResult | Mapping[str, object]],
    *,
    file_format: str = "csv",
    file_stem: str = "resultados_busqueda_invima",
    metadata: Mapping[object, object] | None = None,
    max_rows: int | None = None,
    max_bytes: int = DEFAULT_MAX_EXPORT_BYTES,
) -> ExportArtifact:
    """Construye un artefacto CSV o XLSX con un contrato comun."""

    normalized_format = str(file_format or "").strip().lower().lstrip(".")
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", file_stem).strip("._")
    safe_stem = (safe_stem or "resultados_busqueda_invima")[:120]
    if normalized_format == "csv":
        bounded_rows = DEFAULT_MAX_CSV_ROWS if max_rows is None else max_rows
        exported_rows = 0

        def counted_results():
            nonlocal exported_rows
            for result in results:
                exported_rows += 1
                yield result

        counted = counted_results()
        data = (
            _search_results_with_metadata_to_csv(
                counted,
                metadata,
                max_rows=bounded_rows,
                max_bytes=max_bytes,
            )
            if metadata
            else search_results_to_csv(
                counted,
                max_rows=bounded_rows,
                max_bytes=max_bytes,
            )
        )
        return ExportArtifact(
            data=data,
            file_name=f"{safe_stem}.csv",
            mime_type=CSV_MIME_TYPE,
            row_count=exported_rows,
            file_format="csv",
        )
    if normalized_format == "xlsx":
        bounded_rows = DEFAULT_MAX_XLSX_ROWS if max_rows is None else max_rows
        if bounded_rows < 1:
            raise ValueError("max_rows debe ser mayor que cero")
        materialized = list(islice(results, bounded_rows + 1))
        if len(materialized) > bounded_rows:
            raise ExportLimitError(
                "La exportacion XLSX supera el limite de "
                f"{bounded_rows} filas; acota la consulta"
            )
        data = search_results_to_xlsx(
            materialized,
            metadata=metadata,
            max_rows=bounded_rows,
            max_bytes=max_bytes,
        )
        return ExportArtifact(
            data=data,
            file_name=f"{safe_stem}.xlsx",
            mime_type=XLSX_MIME_TYPE,
            row_count=len(materialized),
            file_format="xlsx",
        )
    raise ValueError("Formato de exportacion no permitido; usa csv o xlsx")
