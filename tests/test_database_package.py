from __future__ import annotations

import gzip
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.database_package import resolve_database_path


class DatabasePackageTests(unittest.TestCase):
    def test_materializes_split_gzip_database(self) -> None:
        payload = b"SQLite format 3\x00" + (b"database-bytes" * 20)
        compressed = gzip.compress(payload)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            runtime_dir = root / "runtime"
            data_dir.mkdir()
            database_path = data_dir / "actas.db"
            midpoint = len(compressed) // 2
            (data_dir / "actas.db.gz.part-000").write_bytes(compressed[:midpoint])
            (data_dir / "actas.db.gz.part-001").write_bytes(compressed[midpoint:])

            with patch.dict(
                os.environ,
                {
                    "ACTAS_RUNTIME_DATA_DIR": str(runtime_dir),
                    "ACTAS_DISABLE_DATABASE_PACKAGE": "0",
                },
            ):
                resolved = resolve_database_path(database_path)

            self.assertEqual(resolved, runtime_dir / "actas.db")
            self.assertEqual(resolved.read_bytes(), payload)

    def test_returns_raw_path_when_no_package_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "actas.db"
            self.assertEqual(resolve_database_path(database_path), database_path)


if __name__ == "__main__":
    unittest.main()
