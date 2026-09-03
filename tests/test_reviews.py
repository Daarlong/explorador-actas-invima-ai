from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from services.reviews import (
    ReviewEvent,
    apply_latest_reviews,
    effective_record,
    latest_reviews,
    load_review_events,
    new_review_event,
    parse_review_events,
    serialize_review_events,
    write_review_events,
)


class ReviewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.record = {
            "decision_uid": "dec_123",
            "record_key": "record-hash",
            "document_hash": "document-hash",
            "product_name": "Producto automático",
            "outcome_code": "sin_clasificar",
            "page_number": 8,
            "end_page_number": 9,
        }

    def test_round_trip_and_effective_overlay(self) -> None:
        event = new_review_event(
            self.record,
            status="reviewed",
            reviewer="Regulatorio Uno",
            notes="Verificado contra el PDF",
            corrections={"product_name": "Producto corregido", "outcome_code": "aprobado"},
            reviewed_at="2026-09-02T12:00:00+00:00",
        )
        restored = parse_review_events(serialize_review_events([event]))
        effective = apply_latest_reviews([self.record], restored)[0]

        self.assertEqual(effective["product_name"], "Producto corregido")
        self.assertEqual(effective["review_status"], "reviewed")
        self.assertEqual(effective["automatic_values"]["product_name"], "Producto automático")

    def test_reextraction_preserves_review_and_pdf_change_marks_it_stale(self) -> None:
        event = new_review_event(
            self.record,
            status="approved",
            reviewer="Revisor",
            notes="",
            corrections={"product_name": "Corregido"},
            reviewed_at="2026-09-02T12:00:00+00:00",
        )
        changed = dict(self.record, record_key="new-record-hash")
        effective = effective_record(changed, event)

        self.assertFalse(effective["review_stale"])
        self.assertTrue(effective["review_needs_reconfirmation"])
        self.assertEqual(effective["review_status"], "approved")
        self.assertEqual(effective["product_name"], "Corregido")

        changed_pdf = dict(changed, document_hash="new-document-hash")
        stale = effective_record(changed_pdf, event)
        self.assertTrue(stale["review_stale"])
        self.assertEqual(stale["review_status"], "stale")
        self.assertEqual(stale["product_name"], "Producto automático")

    def test_latest_review_is_ordered_by_utc_instant(self) -> None:
        older_with_later_wall_clock = ReviewEvent(
            event_id="event-a",
            decision_uid="dec_123",
            source_record_key="record-hash",
            source_document_hash="document-hash",
            status="reviewed",
            reviewer="Revisor anterior",
            reviewed_at="2026-09-02T10:00:00+05:00",
            notes="",
            corrections={},
        )
        newer_in_utc = ReviewEvent(
            event_id="event-b",
            decision_uid="dec_123",
            source_record_key="record-hash",
            source_document_hash="document-hash",
            status="approved",
            reviewer="Revisor vigente",
            reviewed_at="2026-09-02T06:00:00+00:00",
            notes="",
            corrections={},
        )

        current = latest_reviews([newer_in_utc, older_with_later_wall_clock])

        self.assertEqual(current["dec_123"].event_id, "event-b")
        self.assertEqual(current["dec_123"].reviewer, "Revisor vigente")

    def test_rejects_empty_source_fingerprints(self) -> None:
        for missing_field in ("record_key", "document_hash"):
            with self.subTest(api_field=missing_field):
                with self.assertRaisesRegex(ValueError, "huellas"):
                    new_review_event(
                        {**self.record, missing_field: ""},
                        status="reviewed",
                        reviewer="Revisor",
                        notes="",
                        corrections={},
                    )

        valid_event = new_review_event(
            self.record,
            status="approved",
            reviewer="Revisor",
            notes="",
            corrections={},
            reviewed_at="2026-09-02T12:00:00+00:00",
        )
        valid_payload = serialize_review_events([valid_event])
        for fingerprint in ("record-hash", "document-hash"):
            with self.subTest(csv_fingerprint=fingerprint):
                with self.assertRaisesRegex(ValueError, "huellas"):
                    parse_review_events(valid_payload.replace(fingerprint, "", 1))

    def test_rejects_invalid_page_range_and_import_identity(self) -> None:
        with self.assertRaises(ValueError):
            new_review_event(
                self.record,
                status="reviewed",
                reviewer="Revisor",
                notes="",
                corrections={"page_number": 10},
            )
        payload = serialize_review_events(
            [
                new_review_event(
                    self.record,
                    status="reviewed",
                    reviewer="Revisor",
                    notes="",
                    corrections={},
                    reviewed_at="2026-09-02T12:00:00+00:00",
                )
            ]
        ).replace("dec_123", "", 1)
        with self.assertRaises(ValueError):
            parse_review_events(payload)

    def test_writes_atomically(self) -> None:
        event = new_review_event(
            self.record,
            status="reviewed",
            reviewer="Revisor",
            notes="",
            corrections={},
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reviews.csv"
            write_review_events([event], path)
            self.assertEqual(load_review_events(path), [event])

    def test_rejects_unknown_type_and_page_outside_pdf(self) -> None:
        record = dict(self.record, pdf_page_count=9)
        with self.assertRaisesRegex(ValueError, "Tipo de solicitud"):
            new_review_event(
                record,
                status="reviewed",
                reviewer="Revisor",
                notes="",
                corrections={"request_type_code": "tipo_inventado"},
            )
        with self.assertRaisesRegex(ValueError, "9 páginas"):
            new_review_event(
                record,
                status="reviewed",
                reviewer="Revisor",
                notes="",
                corrections={"end_page_number": 10},
            )


if __name__ == "__main__":
    unittest.main()
