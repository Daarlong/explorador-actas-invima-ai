from __future__ import annotations

import gzip
import json
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from services.database_package import (
    create_database_package,
    materialize_database_package,
    package_manifest_path,
    resolve_database_path,
)


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

    def test_creates_verified_deterministic_split_package(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "actas.db"
            with sqlite3.connect(database_path) as connection:
                connection.execute("CREATE TABLE sample (value TEXT)")
                connection.executemany(
                    "INSERT INTO sample (value) VALUES (?)",
                    [("texto regulatorio " * 50,) for _ in range(100)],
                )

            package = create_database_package(
                database_path,
                part_size_bytes=1024,
                delete_source=True,
            )
            self.assertFalse(database_path.exists())
            self.assertGreater(len(package["parts"]), 1)
            self.assertTrue(package_manifest_path(database_path).exists())
            stored = json.loads(
                package_manifest_path(database_path).read_text(encoding="utf-8")
            )
            self.assertEqual(stored["database_sha256"], package["database_sha256"])

            restored = materialize_database_package(database_path, database_path)
            with sqlite3.connect(restored) as connection:
                count = connection.execute("SELECT COUNT(*) FROM sample").fetchone()[0]
            self.assertEqual(count, 100)

    def test_rejects_modified_package_part(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database_path = Path(directory) / "actas.db"
            with sqlite3.connect(database_path) as connection:
                connection.execute("CREATE TABLE sample (value TEXT)")
            package = create_database_package(database_path, part_size_bytes=1024)
            first_part = database_path.parent / package["parts"][0]["name"]
            first_part.write_bytes(first_part.read_bytes() + b"alterado")
            database_path.unlink()
            with self.assertRaisesRegex(ValueError, "Tamaño incorrecto"):
                materialize_database_package(database_path, database_path)


if __name__ == "__main__":
    unittest.main()
