from __future__ import annotations

import sqlite3
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from services.effective_records import (
    associate_effective_records_to_chunks,
    apply_effective_records,
    apply_reconciliation_plan,
    display_value,
    evidence_results_for_records,
    evidence_selection_payload,
    effective_record,
    effective_record_matches,
    field_metadata,
    field_evidence_details,
    hydrate_field_evidence,
    matching_review_uids,
    plan_review_reconciliation,
    scan_effective_record_page,
)


@dataclass(frozen=True)
class Event:
    event_id: str = "evt-1"
    decision_uid: str = "uid-1"
    source_record_key: str = "rk-1"
    source_document_hash: str = "pdf-1"
    status: str = "reviewed"
    reviewer: str = "Revisor"
    reviewed_at: str = "2026-09-03T10:00:00+00:00"
    notes: str = ""
    corrections: dict[str, object] | None = None


class EffectiveRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.record = {
            "record_id": 1,
            "decision_uid": "uid-1",
            "record_key": "rk-1",
            "document_hash": "pdf-1",
            "product_name": "Ozempic",
            "active_ingredient": None,
            "page_number": 10,
            "end_page_number": 11,
            "confidence": 0.71,
            "extraction_method": "legacy",
        }

    def test_combines_evidence_inference_and_human_review(self) -> None:
        record = {
            **self.record,
            "inferred_values_json": '{"principio_activo":"semaglutida"}',
            "field_evidence": [
                {
                    "campo": "producto",
                    "valor_literal": "OZEMPIC",
                    "metodo": "explicit_label",
                    "confianza": 0.98,
                    "pagina": 10,
                    "fragmento": "Producto: OZEMPIC",
                }
            ],
        }
        event = Event(corrections={"active_ingredient": "Semaglutida"})

        effective = effective_record(record, event)

        self.assertEqual(effective["product_name"], "Ozempic")
        self.assertEqual(effective["active_ingredient"], "Semaglutida")
        self.assertEqual(
            effective["field_metadata"]["product_name"]["provenance"], "explicit"
        )
        self.assertEqual(
            effective["field_metadata"]["active_ingredient"]["provenance"],
            "verified",
        )
        details = field_evidence_details(effective, ["product_name"])
        self.assertEqual(details[0]["literal_value"], "OZEMPIC")
        self.assertEqual(details[0]["page"], 10)
        self.assertEqual(effective["automatic_values"]["active_ingredient"], None)
        self.assertEqual(effective["inferred_values"]["active_ingredient"], "semaglutida")

    def test_reextraction_keeps_correction_but_requests_reconfirmation(self) -> None:
        changed = {**self.record, "record_key": "rk-new"}
        event = Event(corrections={"product_name": "Producto humano"})

        effective = effective_record(changed, event)

        self.assertEqual(effective["product_name"], "Producto humano")
        self.assertEqual(effective["review_status"], "reviewed")
        self.assertFalse(effective["review_stale"])
        self.assertTrue(effective["review_extraction_changed"])
        self.assertTrue(effective["review_needs_reconfirmation"])

    def test_changed_pdf_never_receives_old_correction(self) -> None:
        changed = {**self.record, "document_hash": "different-pdf"}
        event = Event(corrections={"product_name": "Producto humano"})

        effective = effective_record(changed, event)

        self.assertEqual(effective["product_name"], "Ozempic")
        self.assertEqual(effective["review_status"], "stale")
        self.assertEqual(effective["review_stale_reason"], "document_changed")

    def test_filters_use_corrected_values_without_reindexing(self) -> None:
        event = Event(corrections={"active_ingredient": "Semaglutida"})
        effective = apply_effective_records([self.record], {"uid-1": event})[0]

        self.assertTrue(effective_record_matches(effective, query="semaglutida"))
        self.assertTrue(
            effective_record_matches(
                effective,
                active_ingredients=["SEMAGLUTIDA"],
                provenances=["verified"],
                confidence_minimum=0.99,
                review_statuses=["reviewed"],
            )
        )
        self.assertFalse(
            effective_record_matches(effective, missing_field="active_ingredient")
        )

    def test_multi_ingredient_confidence_uses_conservative_minimum(self) -> None:
        record = {
            **self.record,
            "active_ingredient": "Semaglutida; Cianocobalamina",
            "field_evidence": [
                {
                    "campo": "principio_activo",
                    "valor_literal": "Semaglutida",
                    "metodo": "explicit_label",
                    "confianza": 0.98,
                    "pagina": 10,
                    "ordinal": 0,
                },
                {
                    "campo": "principio_activo",
                    "valor_literal": "Cianocobalamina",
                    "metodo": "alias_dictionary",
                    "confianza": 0.61,
                    "pagina": 10,
                    "ordinal": 1,
                },
            ],
        }

        effective = effective_record(record)

        self.assertEqual(
            field_metadata(effective, "active_ingredient")["confidence"], 0.61
        )
        self.assertEqual(
            field_metadata(effective, "active_ingredient")["provenance"], "mixed"
        )
        self.assertEqual(len(field_evidence_details(effective, ["active_ingredient"])), 2)
        self.assertTrue(
            effective_record_matches(effective, provenances=["explicit"])
        )
        self.assertTrue(
            effective_record_matches(effective, provenances=["inferred"])
        )
        self.assertFalse(
            effective_record_matches(
                effective,
                confidence_minimum=0.85,
                provenance_field="active_ingredient",
            )
        )

    def test_maps_derived_and_page_range_evidence_to_effective_fields(self) -> None:
        record = {
            **self.record,
            "session_date": "2018-06-18",
            "request_type_code": "evaluacion_farmacologica",
            "outcome_code": "aprobado",
            "field_evidence": [
                {
                    "campo": "fecha_sesion",
                    "valor_literal": "Fecha de la sesión: 18 de junio de 2018",
                    "valor_normalizado": "2018-06-18",
                    "valor_canonico": "2018-06-18",
                    "pagina": 1,
                    "pagina_final": 1,
                    "fragmento": "Fecha de la sesión: 18 de junio de 2018",
                    "metodo": "session_date_label",
                    "confianza": 0.98,
                },
                {
                    "campo": "tipo_solicitud",
                    "valor_literal": "Evaluación farmacológica",
                    "valor_normalizado": "evaluacion_farmacologica",
                    "valor_canonico": "evaluacion_farmacologica",
                    "pagina": 10,
                    "pagina_final": 10,
                    "fragmento": "Solicitud: Evaluación farmacológica",
                    "metodo": "request_type_inference",
                    "confianza": 0.85,
                },
                {
                    "campo": "resultado_normalizado",
                    "valor_literal": "La Sala aprueba la solicitud",
                    "valor_normalizado": "aprobado",
                    "valor_canonico": "aprobado",
                    "pagina": 11,
                    "pagina_final": 11,
                    "fragmento": "Concepto: La Sala aprueba la solicitud",
                    "metodo": "outcome_inference",
                    "confianza": 0.85,
                },
                {
                    "campo": "rango_paginas",
                    "valor_literal": "10-11",
                    "valor_normalizado": "10-11",
                    "valor_canonico": "10-11",
                    "pagina": 10,
                    "pagina_final": 11,
                    "fragmento": "Páginas 10-11",
                    "metodo": "page_span",
                    "confianza": 1.0,
                },
            ],
        }

        effective = effective_record(record)

        self.assertEqual(field_metadata(effective, "session_date")["value"], "2018-06-18")
        self.assertEqual(
            field_metadata(effective, "request_type_code")["provenance"], "inferred"
        )
        self.assertEqual(
            field_metadata(effective, "outcome_code")["canonical_value"], "aprobado"
        )
        self.assertEqual(field_metadata(effective, "page_number")["value"], 10)
        self.assertEqual(field_metadata(effective, "end_page_number")["value"], 11)
        start_detail = field_evidence_details(effective, ["page_number"])[0]
        end_detail = field_evidence_details(effective, ["end_page_number"])[0]
        self.assertEqual(start_detail["source_field"], "rango_paginas")
        self.assertEqual(end_detail["source_field"], "rango_paginas")
        self.assertEqual(start_detail["normalized_value"], "10-11")
        self.assertEqual(start_detail["canonical_value"], "10-11")

    def test_empty_human_correction_is_a_verified_missing_value(self) -> None:
        record = {**self.record, "active_ingredient": "Semaglutida"}
        event = Event(corrections={"active_ingredient": ""})
        effective = effective_record(record, event)

        self.assertTrue(
            effective_record_matches(effective, missing_field="principio_activo")
        )
        self.assertEqual(
            field_metadata(effective, "active_ingredient")["provenance"], "verified"
        )

    def test_human_correction_query_returns_uid(self) -> None:
        older = Event(
            event_id="old",
            reviewed_at="2026-09-03T09:00:00+00:00",
            corrections={"active_ingredient": "Liraglutida"},
        )
        current = Event(
            event_id="new",
            corrections={"active_ingredient": "Semaglutida"},
        )
        self.assertEqual(matching_review_uids([current, older], "semaglutida"), {"uid-1"})
        self.assertEqual(matching_review_uids([current, older], "liraglutida"), set())

    def test_missing_values_never_render_as_none(self) -> None:
        self.assertEqual(display_value(None), "No extraído")
        self.assertEqual(display_value("None"), "No extraído")
        self.assertEqual(display_value([]), "No extraído")
        self.assertEqual(display_value(0), 0)

    def test_strict_reconciliation_uses_both_fingerprints(self) -> None:
        event = Event(decision_uid="old-uid")
        record = {**self.record, "decision_uid": "new-uid"}

        plan = plan_review_reconciliation([event], [record])
        reconciled = apply_reconciliation_plan([event], plan)

        self.assertEqual(plan.uid_mapping, {"old-uid": "new-uid"})
        self.assertEqual(reconciled[0].decision_uid, "new-uid")

        ambiguous = plan_review_reconciliation(
            [event],
            [record, {**record, "decision_uid": "other-uid"}],
        )
        self.assertEqual(ambiguous.uid_mapping, {})
        self.assertEqual(ambiguous.ambiguous_event_ids, ("evt-1",))

    def test_hydrates_v4_field_evidence_and_supports_legacy_database(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "actas.db"
            connection = sqlite3.connect(path)
            connection.execute("CREATE TABLE placeholder (id INTEGER)")
            connection.commit()
            connection.close()
            legacy = hydrate_field_evidence(path, [self.record])
            self.assertNotIn("field_evidence", legacy[0])

            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE regulatory_field_evidence (
                    record_id INTEGER, field_name TEXT, ordinal INTEGER,
                    literal_value TEXT, normalized_value TEXT,
                    canonical_value TEXT, page_number INTEGER,
                    end_page_number INTEGER, evidence_text TEXT,
                    extraction_method TEXT, confidence REAL
                )
                """
            )
            connection.execute(
                "INSERT INTO regulatory_field_evidence VALUES "
                "(1, 'active_ingredient', 0, 'Semaglutida', 'semaglutida', "
                "'Semaglutida', 10, 10, 'Principio activo: Semaglutida', "
                "'explicit_label', 0.99)"
            )
            connection.commit()
            connection.close()

            hydrated = hydrate_field_evidence(path, [self.record])
            effective = effective_record(hydrated[0])

        self.assertEqual(effective["active_ingredient"], "Semaglutida")
        self.assertEqual(
            field_metadata(effective, "active_ingredient")["fragment"],
            "Principio activo: Semaglutida",
        )

    def test_page_correction_reassociates_record_to_new_chunk(self) -> None:
        moved = effective_record(
            self.record,
            Event(corrections={"page_number": 12, "end_page_number": 12}),
        )
        grouped = associate_effective_records_to_chunks(
            {10: [moved], 12: []},
            {
                10: {"page": 10, "url": "https://invima.test/acta.pdf"},
                12: {"page": 12, "url": "https://invima.test/acta.pdf"},
            },
            [{**moved, "url": "https://invima.test/acta.pdf"}],
        )

        self.assertEqual(grouped[10], [])
        self.assertEqual([item["decision_uid"] for item in grouped[12]], ["uid-1"])

    def test_effective_filters_paginate_beyond_first_hundred_records(self) -> None:
        records = [
            {
                **self.record,
                "record_id": index,
                "decision_uid": f"uid-{index}",
                "record_key": f"rk-{index}",
                "active_ingredient": f"Ingrediente {index}",
                "field_evidence": [
                    {
                        "campo": "principio_activo",
                        "valor_literal": f"Ingrediente {index}",
                        "metodo": "explicit_label",
                        "confianza": 0.9,
                        "pagina": 10,
                    }
                ],
            }
            for index in range(250)
        ]

        def fake_list(_path, *, limit, offset, **_filters):
            return records[offset : offset + limit]

        with patch("services.database.list_regulatory_records", side_effect=fake_list):
            page = scan_effective_record_page(
                Path("/ruta/inexistente.db"),
                {},
                page=2,
                page_size=100,
                batch_size=40,
                provenances=["explicit"],
            )

        self.assertEqual(page.total_matches, 250)
        self.assertEqual(len(page.records), 100)
        self.assertEqual(page.records[0]["record_id"], 100)
        self.assertFalse(page.truncated)

    def test_human_only_match_gets_selectable_current_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "actas.db"
            connection = sqlite3.connect(path)
            connection.executescript(
                """
                CREATE TABLE documents (
                    id INTEGER PRIMARY KEY, title TEXT, url TEXT, year INTEGER,
                    acta_number TEXT, section TEXT, part TEXT, source_type TEXT
                );
                CREATE TABLE pages (
                    id INTEGER PRIMARY KEY, document_id INTEGER, page_number INTEGER
                );
                CREATE TABLE chunks (
                    id INTEGER PRIMARY KEY, page_id INTEGER, chunk_index INTEGER,
                    text TEXT
                );
                INSERT INTO documents VALUES
                    (7, 'Acta 14 de 2017', 'https://invima.test/a.pdf', 2017,
                     '14', 'SEMPB', '', 'official');
                INSERT INTO pages VALUES (8, 7, 12);
                INSERT INTO chunks VALUES
                    (9, 8, 0, 'Evidencia oficial de la decisión');
                """
            )
            connection.commit()
            connection.close()
            results, truncated = evidence_results_for_records(
                path,
                [
                    {
                        **self.record,
                        "document_id": 7,
                        "page_number": 12,
                        "end_page_number": 12,
                    }
                ],
            )

        self.assertFalse(truncated)
        self.assertEqual(results["uid-1"].chunk_id, 9)
        self.assertEqual(results["uid-1"].match_type, "human_review")
        payload = evidence_selection_payload(results["uid-1"], "uid-1")
        self.assertEqual(payload["decision_uid"], "uid-1")
        self.assertEqual(payload["chunk_id"], 9)


if __name__ == "__main__":
    unittest.main()
