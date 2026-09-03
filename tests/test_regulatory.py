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
    normalize_request_type,
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
        self.assertEqual(record.numeral, "3.1.2")
        self.assertEqual(record.tipo_solicitud, "evaluacion_farmacologica")

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

    def test_extracts_labeled_session_date_and_keeps_numbered_text(self) -> None:
        records = extract_regulatory_records(
            [
                {
                    "page": 1,
                    "text": "Fecha de la sesión: 15 de enero de 2026",
                },
                {
                    "page": 8,
                    "text": (
                        "3.2.1. EVALUACIONES FARMACOLÓGICAS\n"
                        "Producto: MEDICAMENTO A\nExpediente: 12345\n"
                        "Solicitud: Renovación del registro sanitario.\n"
                        "Concepto: La Sala requiere:\n1.2\n"
                        "Presentar información complementaria."
                    ),
                },
            ]
        )

        self.assertEqual(records[0].fecha_sesion, "2026-01-15")
        self.assertEqual(records[0].numeral, "3.2.1")
        self.assertIn("1.2", records[0].concepto or "")
        self.assertEqual(records[0].tipo_solicitud, "renovacion_registro")

    def test_normalizes_common_request_types(self) -> None:
        self.assertEqual(
            normalize_request_type("Modificación del registro sanitario"),
            "modificacion_registro",
        )
        self.assertEqual(normalize_request_type("texto no categorizado"), "otra_solicitud")

    def test_extracts_semaglutide_from_multiline_historical_composition(self) -> None:
        # Caso minimo inspirado en una publicacion historica; el texto se
        # parafrasea y conserva solo los rotulos necesarios para la regresion.
        records = extract_regulatory_records(
            [
                {
                    "page": 2,
                    "text": (
                        "3.1.1.1 OZEMPIC®\n"
                        "Expediente: 20125116\n"
                        "Radicado: 2017041330\n"
                        "Interesado: Novo Nordisk Colombia S.A.S.\n"
                        "Composición:\nCada mL contiene 1.34mg de\nSemaglutida\n"
                        "SOLICITUD DEL PETICIONARIO: Evaluación farmacológica.\n"
                        "CONCEPTO SALA ESPECIALIZADA: La Sala emite concepto "
                        "favorable."
                    ),
                }
            ]
        )

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.numeral, "3.1.1.1")
        self.assertEqual(record.titulo_numeral, "OZEMPIC®")
        self.assertEqual(record.producto, "OZEMPIC®")
        self.assertEqual(record.principio_activo, "Semaglutida")
        self.assertEqual(record.principios_activos_canonicos, ("Semaglutida",))
        active_evidence = record.evidence_for("principio_activo")
        self.assertEqual(len(active_evidence), 1)
        self.assertEqual(active_evidence[0].metodo, "dosage_statement")
        self.assertIn("Semaglutida", active_evidence[0].fragmento)
        self.assertEqual(
            record.evidence_for("producto")[0].metodo,
            "numbered_heading_product",
        )

    def test_historical_dosage_containers_do_not_pollute_ingredient(self) -> None:
        variants = {
            "Cada tableta recubierta contiene Semaglutida 14 mg": "Semaglutida",
            "Cada frasco contiene Liraglutida 6 mg/mL": "Liraglutida",
            "Cada gramo contiene Dapagliflozina 5 mg": "Dapagliflozina",
            "Cada 1 mL de solución contiene Empagliflozina 10 mg": "Empagliflozina",
        }
        for statement, expected in variants.items():
            with self.subTest(statement=statement):
                record = extract_regulatory_records(
                    [
                        {
                            "page": 12,
                            "text": (
                                "3.1.1 PRODUCTO HISTÓRICO\nProducto: PRUEBA\n"
                                f"{statement}\nExpediente: 12345\n"
                                "Solicitud: Evaluación farmacológica.\n"
                                "Concepto: Se aprueba la solicitud."
                            ),
                        }
                    ]
                )[0]

                self.assertEqual(record.principio_activo, expected)
                evidence = record.evidence_for("principio_activo")[0]
                self.assertEqual(evidence.metodo, "dosage_statement")
                self.assertNotIn("Cada", evidence.valor_literal or "")
                self.assertNotIn("contiene", evidence.valor_literal or "")

        record = extract_regulatory_records(
            [
                {
                    "page": 12,
                    "text": (
                        "3.1.2 PRODUCTO SIN EXCIPIENTE\nProducto: PRUEBA\n"
                        "Principio activo: Semaglutida\n"
                        "Cada tableta no contiene lactosa\nExpediente: 12346\n"
                        "Solicitud: Evaluación farmacológica.\n"
                        "Concepto: Se aprueba la solicitud."
                    ),
                }
            ]
        )[0]
        self.assertEqual(record.principio_activo, "Semaglutida")

    def test_complete_evidence_for_context_and_derived_fields(self) -> None:
        record = extract_regulatory_records(
            [
                {
                    "page": 1,
                    "text": "Fecha de la sesión: 18 de junio de 2018",
                },
                {
                    "page": 7,
                    "text": (
                        "3.1.1 OZEMPIC\nProducto: OZEMPIC\n"
                        "Cada frasco contiene Semaglutida 2 mg\n"
                        "Expediente: 20125116\n"
                        "Solicitud: Evaluación farmacológica.\n"
                        "Concepto: La Sala aprueba la solicitud."
                    ),
                },
            ]
        )[0]

        expected_fields = {
            "titulo_numeral",
            "fecha_sesion",
            "fecha_sesion_original",
            "tipo_solicitud",
            "resultado_normalizado",
            "rango_paginas",
        }
        self.assertTrue(expected_fields.issubset({
            item.campo for item in record.evidencias_campos
        }))
        title = record.evidence_for("titulo_numeral")[0]
        self.assertEqual(title.valor_literal, "OZEMPIC")
        self.assertEqual(title.valor_normalizado, "ozempic")
        self.assertEqual(title.pagina, 7)
        session = record.evidence_for("fecha_sesion")[0]
        self.assertEqual(session.valor_normalizado, "2018-06-18")
        self.assertEqual(session.valor_canonico, "2018-06-18")
        self.assertEqual(session.pagina, 1)
        self.assertIn("Fecha de la sesión", session.fragmento)
        original = record.evidence_for("fecha_sesion_original")[0]
        self.assertEqual(original.valor_literal, "Fecha de la sesión: 18 de junio de 2018")
        request_type = record.evidence_for("tipo_solicitud")[0]
        self.assertEqual(request_type.valor_canonico, "evaluacion_farmacologica")
        self.assertEqual(request_type.metodo, "request_type_inference")
        outcome = record.evidence_for("resultado_normalizado")[0]
        self.assertEqual(outcome.valor_canonico, RESULT_APPROVED)
        self.assertEqual(outcome.metodo, "outcome_inference")
        page_range = record.evidence_for("rango_paginas")[0]
        self.assertEqual((page_range.pagina, page_range.pagina_final), (7, 7))
        for field in expected_fields:
            for evidence in record.evidence_for(field):
                self.assertTrue(evidence.fragmento)
                self.assertTrue(evidence.metodo)
                self.assertGreaterEqual(evidence.confianza, 0.0)
                self.assertLessEqual(evidence.confianza, 1.0)

    def test_supports_multiple_active_ingredients_without_alias_inference(self) -> None:
        records = extract_regulatory_records(
            [
                {
                    "page": 12,
                    "text": (
                        "4.2.1 COMBINACIONES\n"
                        "Producto: COMBINADO X\n"
                        "Composición cualitativa: Insulina degludec 100 U/mL + "
                        "liraglutida 3,6 mg/mL\n"
                        "Expediente: 808080\n"
                        "Solicitud: Modificación del registro sanitario.\n"
                        "Concepto: Se acepta lo solicitado."
                    ),
                }
            ]
        )

        record = records[0]
        self.assertEqual(
            record.principios_activos_canonicos,
            ("Insulina degludec", "liraglutida"),
        )
        self.assertEqual(
            record.principio_activo,
            "Insulina degludec; liraglutida",
        )
        self.assertEqual(
            [item.ordinal for item in record.evidence_for("principio_activo")],
            [1, 2],
        )
        # El extractor no inventa una relacion usando solo el nombre comercial.
        unrelated = extract_regulatory_records(
            [
                {
                    "page": 13,
                    "text": (
                        "Producto: COMBINADO X\nExpediente: 909090\n"
                        "Solicitud: Renovación del registro sanitario.\n"
                        "Concepto: La solicitud es favorable."
                    ),
                }
            ]
        )[0]
        self.assertIsNone(unrelated.principio_activo)
        self.assertEqual(unrelated.principios_activos, ())

    def test_merged_historical_blocks_get_unique_evidence_ordinals(self) -> None:
        pages = [
            {
                "page": 77,
                "text": (
                    "3.1.1 MEDICAMENTO X\n"
                    "Producto: MEDICAMENTO X\n"
                    "Principio activo: semaglutida 1 mg\n"
                    "Expediente: 12345\n"
                    "Solicitud: Registro sanitario\n"
                    "Concepto: Se aprueba.\n"
                    "3.1.2 MEDICAMENTO X\n"
                    "Producto: MEDICAMENTO X\n"
                    "Principios activos: semaglutida 1 mg + cagrilintida 2 mg\n"
                    "Expediente: 12345\n"
                    "Solicitud: Registro sanitario del producto\n"
                    "Concepto: Se aprueba. Se autoriza la comercialización."
                ),
            }
        ]

        first = extract_regulatory_records(pages)
        second = extract_regulatory_records(pages)

        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].as_dict(), second[0].as_dict())
        evidence_keys = [
            (item.campo, item.ordinal) for item in first[0].evidencias_campos
        ]
        self.assertEqual(len(evidence_keys), len(set(evidence_keys)))
        self.assertEqual(
            [item.ordinal for item in first[0].evidence_for("solicitud")],
            [1, 2],
        )
        active_evidence = first[0].evidence_for("principio_activo")
        self.assertEqual([item.ordinal for item in active_evidence], [1, 2, 3])
        self.assertEqual(
            {item.valor_canonico for item in active_evidence},
            {"semaglutida", "cagrilintida"},
        )

    def test_supports_multiline_bulleted_active_ingredients(self) -> None:
        record = extract_regulatory_records(
            [
                {
                    "page": 14,
                    "text": (
                        "4.2.2 COMBINACIÓN DOS\nProducto: COMBINADO Y\n"
                        "Principios activos:\n1. semaglutida 1 mg\n"
                        "2. cagrilintida 2 mg\nExpediente: 1414\n"
                        "Solicitud: Evaluación farmacológica.\n"
                        "Concepto: Se requiere información adicional."
                    ),
                }
            ]
        )[0]

        self.assertEqual(
            tuple(item.casefold() for item in record.principios_activos_canonicos),
            ("semaglutida", "cagrilintida"),
        )

    def test_recognizes_dci_ifa_and_historical_numbering(self) -> None:
        pages = [
            {
                "page": 20,
                "text": (
                    "NUMERAL 7.3 - MEDICAMENTOS DE SÍNTESIS\n"
                    "Producto: MEDICAMENTO D\nD.C.I.: dapagliflozina\n"
                    "Expediente: 7001\nSolicitud: Nueva indicación.\n"
                    "Concepto: Se aprueba la indicación."
                ),
            },
            {
                "page": 21,
                "text": (
                    "IV. EVALUACIONES FARMACOLÓGICAS\n"
                    "Producto: MEDICAMENTO E\nI.F.A.: empagliflozina\n"
                    "Expediente: 7002\nSolicitud: Evaluación farmacológica.\n"
                    "Concepto: Se requiere información adicional."
                ),
            },
        ]

        records = extract_regulatory_records(pages)

        self.assertEqual([record.numeral for record in records], ["7.3", "IV"])
        self.assertEqual(
            [record.principio_activo for record in records],
            ["dapagliflozina", "empagliflozina"],
        )
        self.assertTrue(all(record.evidence_for("numeral") for record in records))

    def test_reads_leaf_numeral_and_product_label_on_the_same_line(self) -> None:
        record = extract_regulatory_records(
            [
                {
                    "page": 22,
                    "text": (
                        "3.1.8.2.- Producto: MEDICAMENTO EN LÍNEA\n"
                        "Ingrediente activo: semaglutida\nExpediente: 8182\n"
                        "Solicitud: Evaluación farmacológica.\n"
                        "Concepto: Se aprueba la solicitud."
                    ),
                }
            ]
        )[0]

        self.assertEqual(record.numeral, "3.1.8.2")
        self.assertEqual(record.producto, "MEDICAMENTO EN LÍNEA")

    def test_repeated_headers_and_footers_do_not_pollute_continuations(self) -> None:
        header = "SALA ESPECIALIZADA DE MEDICAMENTOS - ACTA HISTÓRICA"
        footer = "Instituto Nacional de Vigilancia de Medicamentos y Alimentos"
        pages = [
            {
                "page": 30,
                "text": (
                    f"{header}\n3.4.5 PRODUCTO BIOLÓGICO\nProducto: BIO X\n"
                    "Ingrediente activo: anticuerpo alfa\nExpediente: 3030\n"
                    "Solicitud: Modificación de indicaciones que continúa\n"
                    f"{footer}"
                ),
            },
            {
                "page": 31,
                "text": (
                    f"{header}\nen la página siguiente.\nConcepto: La Sala requiere\n"
                    f"información complementaria.\n{footer}"
                ),
            },
            {"page": 32, "text": f"{header}\nPágina 32 de 40\n{footer}"},
        ]

        record = extract_regulatory_records(pages)[0]

        self.assertNotIn("Instituto Nacional", record.solicitud or "")
        self.assertNotIn("SALA ESPECIALIZADA", record.solicitud or "")
        self.assertIn("página siguiente", record.solicitud or "")
        self.assertEqual(record.pagina_final, 31)
        page_range = record.evidence_for("rango_paginas")[0]
        self.assertEqual((page_range.pagina, page_range.pagina_final), (30, 31))

    def test_numbered_requirements_are_not_mistaken_for_new_records(self) -> None:
        record = extract_regulatory_records(
            [
                {
                    "page": 4,
                    "text": (
                        "3.2.1 EVALUACIÓN\nProducto: FÁRMACO A\nExpediente: 44\n"
                        "Solicitud: Modificación.\nConcepto: La Sala requiere:\n"
                        "1. Presentar el estudio de estabilidad.\n"
                        "2. Aclarar la concentración."
                    ),
                }
            ]
        )[0]

        self.assertEqual(record.numeral, "3.2.1")
        self.assertIn("Presentar el estudio", record.concepto or "")
        self.assertIn("Aclarar la concentración", record.concepto or "")

    def test_uppercase_numbered_requirements_remain_inside_concept(self) -> None:
        record = extract_regulatory_records(
            [
                {
                    "page": 44,
                    "text": (
                        "3.2.8 MEDICAMENTO HISTÓRICO\nInteresado: LABORATORIO A\n"
                        "Expediente: 19990001\nRadicado: 20130000001\n"
                        "Solicitud: Modificación de indicaciones.\n"
                        "Concepto: Se requiere:\n"
                        "1. PRESENTAR INFORMACIÓN DEL PRODUCTO\n"
                        "2. ACLARAR LOS RESULTADOS DEL ESTUDIO"
                    ),
                }
            ]
        )[0]

        self.assertEqual(record.numeral, "3.2.8")
        self.assertIn("PRESENTAR INFORMACIÓN", record.concepto or "")
        self.assertIn("ACLARAR LOS RESULTADOS", record.concepto or "")
        self.assertEqual(record.resultado_normalizado, RESULT_REQUIRED)

    def test_roman_initial_words_do_not_split_historical_blocks(self) -> None:
        records = extract_regulatory_records(
            [
                {
                    "page": 61,
                    "text": (
                        "3.13.20 MEDICAMENTO A\nInteresado:\n"
                        "LABORATORIOS EJEMPLO S.A.S.\nExpediente:\n20052699\n"
                        "Radicado:\n2013076092\nSolicitud:\n"
                        "Modificación del registro sanitario.\nConcepto:\n"
                        "La Sala Especializada de Medicamentos considera que la "
                        "solicitud es procedente.\n"
                        "De acuerdo con la evaluación, se aprueba lo solicitado."
                    ),
                }
            ]
        )

        self.assertEqual(len(records), 1)
        record = records[0]
        self.assertEqual(record.numeral, "3.13.20")
        self.assertEqual(record.interesado, "LABORATORIOS EJEMPLO S.A.S.")
        self.assertEqual(record.expediente, "20052699")
        self.assertEqual(record.radicado, "2013076092")
        self.assertIn("La Sala Especializada", record.concepto or "")
        self.assertIn("De acuerdo", record.concepto or "")
        self.assertEqual(record.resultado_normalizado, RESULT_APPROVED)

    def test_strips_closed_list_of_footer_text_from_identifiers(self) -> None:
        record = extract_regulatory_records(
            [
                {
                    "page": 29,
                    "text": (
                        "3.13.25 MEDICAMENTO B\nInteresado: LAB B\n"
                        "Expediente: 19943627\n"
                        "Radicado: 2013078759 Carrera 68 D 17-11 PBX 2948700 "
                        "Bogotá Colombia\nSolicitud: Nueva indicación.\n"
                        "Concepto: Se aprueba la indicación."
                    ),
                }
            ]
        )[0]

        self.assertEqual(record.radicado, "2013078759")
        evidence = record.evidence_for("radicado")[0]
        self.assertEqual(evidence.valor_literal, "2013078759")

        compact = extract_regulatory_records(
            [
                {
                    "page": 30,
                    "text": (
                        "3.13.26 MEDICAMENTO B2\nInteresado: LAB B\n"
                        "Expediente: 19943628\n"
                        "Radicado: 2013078760ELFORMATOIMPRESODEESTEDOCUMENTOESUNA"
                        "COPIANOCONTROLADA F07-PM05\n"
                        "Solicitud: Nueva indicación.\n"
                        "Concepto: Se aprueba la indicación."
                    ),
                }
            ]
        )[0]
        self.assertEqual(compact.radicado, "2013078760")

    def test_accepts_conservative_historical_identity_label_variants(self) -> None:
        record = extract_regulatory_records(
            [
                {
                    "page": 9,
                    "text": (
                        "NUMERAL 3.1.9 MEDICAMENTO C\n"
                        "INTERESADO(S): LABORATORIOS C\n"
                        "EXPEDIENTE(S) No.: 20001234\n"
                        "NÚMERO DE RADICACIÓN: 20131234567\n"
                        "Solicitud: Evaluación farmacológica.\n"
                        "Concepto: La Sala emite concepto favorable."
                    ),
                }
            ]
        )[0]

        self.assertEqual(record.interesado, "LABORATORIOS C")
        self.assertEqual(record.expediente, "20001234")
        self.assertEqual(record.radicado, "20131234567")
        self.assertEqual(record.tipo_solicitud, "evaluacion_farmacologica")
        self.assertEqual(record.resultado_normalizado, "favorable")

    def test_field_evidence_is_serializable_and_bounded(self) -> None:
        record = extract_regulatory_records(
            [
                {
                    "page": 6,
                    "text": (
                        "5.1 PRODUCTOS\nProducto: MEDICAMENTO P\n"
                        "Principio activo: semaglutida\nExpediente: 123\n"
                        "Solicitud: Registro sanitario.\nConcepto: Se aprueba."
                    ),
                }
            ]
        )[0]

        serialized = record.as_dict()
        json.dumps(serialized, ensure_ascii=False)
        product = record.evidence_for("producto")[0]
        self.assertEqual(product.valor_literal, "MEDICAMENTO P")
        self.assertEqual(product.metodo, "explicit_label")
        self.assertGreaterEqual(product.confianza, 0.0)
        self.assertLessEqual(product.confianza, 1.0)


if __name__ == "__main__":
    unittest.main()
