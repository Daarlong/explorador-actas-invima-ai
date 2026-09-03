from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from services.database import initialize_database


class EvaluationCliTests(unittest.TestCase):
    def test_empty_bank_is_advisory_normally_and_blocks_release(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "actas.db"
            cases = root / "cases.csv"
            integrity = root / "integrity.json"
            initialize_database(database)
            cases.write_text(
                "case_id,query,expected_refs,filters_json,k,exact_phrase,notes,enabled\n",
                encoding="utf-8",
            )
            integrity.write_text(
                json.dumps({"status": "ok"}),
                encoding="utf-8",
            )
            environment = {
                **os.environ,
                "ACTAS_DISABLE_DATABASE_PACKAGE": "1",
                "ACTAS_DATABASE_PATH": str(database),
                "ACTAS_SEMANTIC_INDEX_PATH": str(root / "semantic.db"),
                "ACTAS_INTEGRITY_REPORT_PATH": str(integrity),
                "ACTAS_EVALUATION_CASES_PATH": str(cases),
            }

            advisory_path = root / "advisory.json"
            advisory = subprocess.run(
                [
                    sys.executable,
                    "evaluate_search.py",
                    "--allow-empty",
                    "--output",
                    str(advisory_path),
                ],
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            release_path = root / "release.json"
            release = subprocess.run(
                [
                    sys.executable,
                    "evaluate_search.py",
                    "--allow-empty",
                    "--release",
                    "--output",
                    str(release_path),
                ],
                cwd=project_root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )

            advisory_report = json.loads(advisory_path.read_text(encoding="utf-8"))
            release_report = json.loads(release_path.read_text(encoding="utf-8"))

        self.assertEqual(advisory.returncode, 0, advisory.stderr)
        self.assertEqual(advisory_report["quality_gate"]["status"], "advisory")
        self.assertTrue(advisory_report["quality_gate"]["can_publish"])
        self.assertNotEqual(release.returncode, 0)
        self.assertEqual(release_report["quality_gate"]["status"], "fail")
        self.assertFalse(release_report["quality_gate"]["can_publish"])


if __name__ == "__main__":
    unittest.main()
