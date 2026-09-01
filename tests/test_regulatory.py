import json
import unittest

from services.regulatory import (
    RESULT_APPROVED,
    RESULT_ARCHIVED,
    RESULT_DENIED,
    RESULT_REQUIRED,
    RegulatoryRecord,
    extract_regulatory_dicts,
    extract_regulatory_records,
    normalize_regulatory_result,
)


class RegulatoryExtractionTests(unittest.TestCase):
    def test_extracts_complete_realistic_decision(self) -> None:
        pages = [
            {
                "page": 18,
                "text": """
                3.1.2. EVALUACIONES FARMACOLÓGICAS
                Producto: OZEMPIC 1,34 mg/mL
                Principio activo: Semaglutida
                Interesado: NOVO NORDISK A/S
                Expediente: 20234567
                Radicado: 20261234567
                Solicitud: Evaluación farmacológica de una nueva concentración.
                Concepto: Revisada la documentación allegada, la Sala considera
                que es procedente aprobar la nueva concentración solicitada.
                """,
            }
        ]

        records = extract_regulatory_records(pages)

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertIsInstance(record, RegulatoryRecord)
        self.assertEqual(record.producto, "OZEMPIC 1,34 mg/mL")
        self.assertEqual(record.principio_activo, "Semaglutida")
        self.assertEqual(record.interesado, "NOVO NORDISK A/S")
        self.assertEqual(record.expediente, "20234567")
        self.assertEqual(record.radicado, "20261234567")
        self.assertIn("nueva concentración", record.solicitud or "")
        self.assertIn("documentación allegada", record.concepto or "")
        self.assertEqual(record.resultado_normalizado, RESULT_APPROVED)
        self.assertEqual(record.pagina, 18)
        self.assertEqual(record.page, 18)

    def test_accepts_label_variants_and_continuations_between_pages(self) -> None:
        pages = [
            {
                "page": 41,
                "text": """
                NOMBRE DEL MEDICAMENTO - Producto Biológico Alfa
                INGREDIENTES ACTIVOS: anticuerpo monoclonal alfa
                Solicitante:
                Laboratorios Ejemplo S.A.S.
                N.º expediente: 19999999
                Número de radicado: 20250001234
                SOLICITUD DEL INTERESADO:
                Modificación de indicaciones y actualización de la
                """,
            },
            {
                "page": 42,
                "text": """
                página 42 de 120
                información para prescribir presentada en el expediente.
                Fabricante: Ejemplo Biologics Ltd.
                CONCEPTO DE LA SALA ESPECIALIZADA:
                Luego de evaluar la respuesta, se requiere al interesado
                allegar el análisis de inmunogenicidad solicitado.
                """,
            },
        ]

        records = extract_regulatory_records(pages)

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.producto, "Producto Biológico Alfa")
        self.assertEqual(record.principio_activo, "anticuerpo monoclonal alfa")
        self.assertEqual(record.interesado, "Laboratorios Ejemplo S.A.S.")
        self.assertEqual(record.expediente, "19999999")
        self.assertEqual(record.radicado, "20250001234")
        self.assertIn("información para prescribir", record.solicitud or "")
        self.assertNotIn("Fabricante", record.solicitud or "")
        self.assertIn("análisis de inmunogenicidad", record.concepto or "")
        self.assertEqual(record.resultado_normalizado, RESULT_REQUIRED)
        self.assertEqual(record.pagina, 41)

    def test_separates_records_and_deduplicates_repeated_block(self) -> None:
        repeated = """
        Producto: MEDICAMENTO A
        Expediente: 11111
        Radicado: 20260000001
        Solicitud: Renovación del registro sanitario.
        Concepto: La Sala conceptúa favorablemente la solicitud.
        """
        pages = [
            {"page": 5, "text": repeated},
            {"page": 6, "text": repeated},
            {
                "page": 7,
                "text": """
                Producto: MEDICAMENTO A
                Expediente: 22222
                Radicado: 20260000002
                Solicitud: Modificación de indicaciones.
                Concepto: No es procedente aprobar la modificación solicitada.
                """,
            },
        ]

        records = extract_regulatory_records(pages)

        self.assertEqual(len(records), 2)
        self.assertEqual(records[0].radicado, "20260000001")
        self.assertEqual(records[0].pagina, 5)
        self.assertEqual(records[1].radicado, "20260000002")
        self.assertEqual(records[1].resultado_normalizado, RESULT_DENIED)

    def test_reads_multiple_labels_on_one_line(self) -> None:
        pages = [
            {
                "page": 9,
                "text": (
                    "Producto: FÁRMACO X  Expediente: EXP-88  Radicado: RAD-99\n"
                    "Solicitud: Cambio de fabricante.  Decisión: Se acepta lo solicitado."
                ),
            }
        ]

        records = extract_regulatory_records(pages)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].producto, "FÁRMACO X")
        self.assertEqual(records[0].expediente, "EXP-88")
        self.assertEqual(records[0].radicado, "RAD-99")
        self.assertEqual(records[0].resultado_normalizado, RESULT_APPROVED)

    def test_omits_isolated_labels_without_regulatory_evidence(self) -> None:
        pages = [
            {
                "page": 1,
                "text": "Acta No. 08 de 2026\nProducto: encabezado general\nPágina 1 de 90",
            }
        ]

        self.assertEqual(extract_regulatory_records(pages), [])

    def test_serializable_dictionary_api(self) -> None:
        records = extract_regulatory_dicts(
            [
                {
                    "page": "3",
                    "text": (
                        "Producto: MEDICAMENTO Z\nRadicado: 123\n"
                        "Solicitud: Cancelación voluntaria.\n"
                        "Concepto: Se ordena el archivo del trámite."
                    ),
                }
            ]
        )

        self.assertEqual(records[0]["resultado_normalizado"], RESULT_ARCHIVED)
        self.assertEqual(records[0]["pagina"], 3)
        json.dumps(records, ensure_ascii=False)

    def test_negative_wording_takes_precedence_over_approve(self) -> None:
        self.assertEqual(
            normalize_regulatory_result(
                "Con la información actual no es procedente aprobar la solicitud."
            ),
            RESULT_DENIED,
        )
        for wording in (
            "La Sala no aprueba la solicitud.",
            "La Sala no se aprueba la modificación.",
            "La Sala no se acepta lo solicitado.",
            "La Sala deniega la petición.",
        ):
            with self.subTest(wording=wording):
                self.assertEqual(
                    normalize_regulatory_result(wording),
                    RESULT_DENIED,
                )

    def test_does_not_treat_no_requirement_as_required(self) -> None:
        self.assertEqual(
            normalize_regulatory_result(
                "La Sala concluye que no se requiere información adicional."
            ),
            "sin_clasificar",
        )

    def test_recognizes_common_present_tense_decisions(self) -> None:
        self.assertEqual(
            normalize_regulatory_result("La Sala aprueba lo solicitado."),
            RESULT_APPROVED,
        )
        self.assertEqual(
            normalize_regulatory_result("La Sala niega la solicitud."),
            RESULT_DENIED,
        )


if __name__ == "__main__":
    unittest.main()
