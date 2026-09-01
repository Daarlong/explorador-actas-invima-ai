from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from build_index import remove_semantic_artifacts


class BuildIndexTests(unittest.TestCase):
    def test_removes_stale_semantic_package_after_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "semantic.db"
            paths = [
                database,
                Path(temporary) / "semantic.db.gz",
                Path(temporary) / "semantic.db.package.json",
                Path(temporary) / "semantic.db.package-id",
                Path(temporary) / "semantic.db.gz.part-000",
                Path(temporary) / "semantic.db.gz.part-001",
            ]
            for path in paths:
                path.write_bytes(b"stale")

            with patch("build_index.SEMANTIC_INDEX_PATH", database):
                remove_semantic_artifacts()

            self.assertTrue(all(not path.exists() for path in paths))


if __name__ == "__main__":
    unittest.main()
