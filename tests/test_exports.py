from __future__ import annotations

import csv
from importlib.util import find_spec
from io import StringIO
import unittest
from zipfile import ZipFile
from io import BytesIO

from services.exports import (
    CSV_MIME_TYPE,
    MAX_EXCEL_CELL_CHARACTERS,
    SEARCH_EXPORT_COLUMNS,
    XLSX_MIME_TYPE,
    ExportLimitError,
    export_search_results,
    rows_to_csv,
    rows_to_xlsx,
    search_results_to_csv,
)
from services.models import SearchResult


def sample_result(**changes) -> SearchResult:
    values = {
        "chunk_id": 17,
        "title": "Acta 08 de 2026",
        "url": "https://www.invima.gov.co/biblioteca/download/17",
        "page": 9,
        "text": "Beneficio clínico\ncon evidencia verificable.",
        "year": 2026,
        "acta_number": "08",
        "section": "SEMPB",
        "part": "1",
        "source_type": "official",
        "score": 0.987,
        "lexical_score": 0.8,
        "semantic_score": 0.7,
        "match_type": "hybrid",
    }
    values.update(changes)
    return SearchResult(**values)


class CsvExportTests(unittest.TestCase):
    def test_search_results_csv_has_bom_and_complete_fragment_fields(self) -> None:
        content = search_results_to_csv([sample_result()])

        self.assertTrue(content.startswith(b"\xef\xbb\xbf"))
        rows = list(csv.DictReader(StringIO(content.decode("utf-8-sig"))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["posicion"], "1")
        self.assertEqual(rows[0]["id_fragmento"], "17")
        self.assertEqual(rows[0]["documento"], "Acta 08 de 2026")
        self.assertEqual(rows[0]["fragmento"], "Beneficio clínico\ncon evidencia verificable.")
        self.assertEqual(rows[0]["tipo_coincidencia"], "hybrid")
        self.assertEqual(rows[0]["puntaje_textual"], "0.8")
        self.assertEqual(rows[0]["puntaje_semantico"], "0.7")
        self.assertEqual(
            list(rows[0]),
            [label for _, label in SEARCH_EXPORT_COLUMNS],
        )

    def test_csv_neutralizes_text_formulas_but_preserves_numbers(self) -> None:
        content = rows_to_csv(
            ["formula", "leading", "negative_text", "at", "number"],
            [["=2+2", "  +CMD", "-riesgo", "@SUM(A1)", -7]],
        )
        row = next(csv.DictReader(StringIO(content.decode("utf-8-sig"))))

        self.assertEqual(row["formula"], "'=2+2")
        self.assertEqual(row["leading"], "'  +CMD")
        self.assertEqual(row["negative_text"], "'-riesgo")
        self.assertEqual(row["at"], "'@SUM(A1)")
        self.assertEqual(row["number"], "-7")

    def test_csv_rejects_row_and_byte_overflow_without_truncating(self) -> None:
        with self.assertRaisesRegex(ExportLimitError, "1 filas"):
            rows_to_csv(["valor"], [["uno"], ["dos"]], max_rows=1)
        with self.assertRaisesRegex(ExportLimitError, "MiB"):
            rows_to_csv(["valor"], [["contenido extenso"]], max_bytes=10)

    def test_csv_rejects_rows_with_a_different_width(self) -> None:
        with self.assertRaisesRegex(ValueError, "se esperaban 2"):
            rows_to_csv(["a", "b"], [["solo una"]])

    def test_search_export_accepts_mappings_and_counts_multiline_rows(self) -> None:
        artifact = export_search_results(
            [
                sample_result().as_dict(),
                sample_result(chunk_id=18, text="primera\nsegunda").as_dict(),
            ],
            file_format=".CSV",
            file_stem="consulta INVIMA / prueba",
        )

        self.assertEqual(artifact.row_count, 2)
        self.assertEqual(artifact.file_name, "consulta_INVIMA_prueba.csv")
        self.assertEqual(artifact.mime_type, CSV_MIME_TYPE)
        self.assertEqual(artifact.file_format, "csv")
        self.assertEqual(
            len(list(csv.DictReader(StringIO(artifact.data.decode("utf-8-sig"))))),
            2,
        )

    def test_user_csv_repeats_query_metadata_and_evidence_trace(self) -> None:
        artifact = export_search_results(
            [
                sample_result(
                    matched_field="active_ingredient",
                    evidence_scope="regulatory_field",
                    match_excerpt="Semaglutida",
                    document_id=8,
                )
            ],
            metadata={
                "consulta": "Semaglutida",
                "metodo_solicitado": "textual",
                "motor_real": "structured_fts",
                "campo": "active_ingredient",
                "filtros": '{"years": [2026]}',
            },
        )

        row = next(
            csv.DictReader(StringIO(artifact.data.decode("utf-8-sig")))
        )
        self.assertEqual(row["consulta"], "Semaglutida")
        self.assertEqual(row["campo_consultado"], "active_ingredient")
        self.assertEqual(row["id_documento"], "8")
        self.assertEqual(row["campo_coincidente"], "active_ingredient")
        self.assertEqual(row["evidencia_coincidente"], "Semaglutida")

    def test_search_export_rejects_unknown_format(self) -> None:
        with self.assertRaisesRegex(ValueError, "csv o xlsx"):
            export_search_results([], file_format="pdf")


@unittest.skipUnless(find_spec("xlsxwriter"), "XlsxWriter no está instalado")
class XlsxExportTests(unittest.TestCase):
    def test_xlsx_is_valid_and_writes_untrusted_values_as_strings(self) -> None:
        content = rows_to_xlsx(
            ["campo", "valor"],
            [["formula", "=HYPERLINK(\"https://example.com\")"]],
            metadata={"consulta": "@riesgo", "modo": "híbrido"},
        )

        self.assertTrue(content.startswith(b"PK"))
        with ZipFile(BytesIO(content)) as archive:
            xml = b"\n".join(
                archive.read(name)
                for name in archive.namelist()
                if name.endswith(".xml")
            ).decode("utf-8")
        self.assertNotIn("<f>", xml)
        self.assertIn("'=HYPERLINK", xml)
        self.assertIn("'@riesgo", xml)

    def test_xlsx_artifact_reports_type_name_and_row_count(self) -> None:
        artifact = export_search_results(
            [sample_result(), sample_result(chunk_id=19)],
            file_format="xlsx",
            metadata={"alcance": "conjunto recuperado"},
        )

        self.assertEqual(artifact.row_count, 2)
        self.assertEqual(artifact.file_name, "resultados_busqueda_invima.xlsx")
        self.assertEqual(artifact.mime_type, XLSX_MIME_TYPE)
        self.assertEqual(artifact.file_format, "xlsx")

    def test_xlsx_rejects_row_cell_and_byte_overflow(self) -> None:
        with self.assertRaisesRegex(ExportLimitError, "1 filas"):
            rows_to_xlsx(["valor"], [["uno"], ["dos"]], max_rows=1)
        with self.assertRaisesRegex(ExportLimitError, "limite de Excel"):
            rows_to_xlsx(
                ["valor"],
                [["x" * (MAX_EXCEL_CELL_CHARACTERS + 1)]],
            )
        with self.assertRaisesRegex(ExportLimitError, "MiB"):
            rows_to_xlsx(["valor"], [["uno"]], max_bytes=100)

    def test_xlsx_avoids_metadata_sheet_name_collision(self) -> None:
        content = rows_to_xlsx(
            ["valor"],
            [["uno"]],
            sheet_name="Consulta",
            metadata={"modo": "textual"},
        )
        with ZipFile(BytesIO(content)) as archive:
            workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
        self.assertIn('name="Consulta"', workbook_xml)
        self.assertIn('name="Metadatos"', workbook_xml)


if __name__ == "__main__":
    unittest.main()
