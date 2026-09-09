from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from build_index import remove_semantic_checkpoint


class BuildIndexTests(unittest.TestCase):
    def test_removes_only_checkpoint_and_preserves_published_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            published = Path(temporary) / "semantic.db"
            checkpoint = Path(temporary) / "semantic.checkpoint.db"
            checkpoint_paths = [
                checkpoint,
                Path(temporary) / "semantic.checkpoint.db-shm",
                Path(temporary) / "semantic.checkpoint.db-wal",
            ]
            published.write_bytes(b"published")
            for path in checkpoint_paths:
                path.write_bytes(b"stale")

            with patch("build_index.SEMANTIC_CHECKPOINT_PATH", checkpoint):
                remove_semantic_checkpoint()

            self.assertTrue(all(not path.exists() for path in checkpoint_paths))
            self.assertEqual(published.read_bytes(), b"published")


if __name__ == "__main__":
    unittest.main()
