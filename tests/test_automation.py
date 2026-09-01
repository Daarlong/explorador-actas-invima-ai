from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from services.automation import pending_index_items


class AutomationTests(unittest.TestCase):
    def test_missing_report_requests_index_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            status = pending_index_items(Path(directory) / "missing.json")

        self.assertTrue(status["needs_update"])
        self.assertEqual(status["reason"], "missing_integrity_report")

    def test_pending_documents_request_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "integrity.json"
            path.write_text(
                json.dumps(
                    {
                        "missing_documents": ["Acta 01"],
                        "documents_without_pages": [],
                        "documents_without_chunks": [],
                    }
                ),
                encoding="utf-8",
            )
            status = pending_index_items(path)

        self.assertTrue(status["needs_update"])
        self.assertEqual(status["pending_count"], 1)

    def test_complete_report_does_not_request_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "integrity.json"
            path.write_text(
                json.dumps({key: [] for key in (
                    "missing_documents",
                    "documents_without_pages",
                    "documents_without_chunks",
                )}),
                encoding="utf-8",
            )
            status = pending_index_items(path)

        self.assertFalse(status["needs_update"])
        self.assertEqual(status["reason"], "complete")


if __name__ == "__main__":
    unittest.main()
