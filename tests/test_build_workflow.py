from __future__ import annotations

import unittest
from pathlib import Path


WORKFLOW_PATH = (
    Path(__file__).resolve().parents[1] / ".github" / "workflows" / "build-index.yml"
)


class BuildWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.workflow = WORKFLOW_PATH.read_text(encoding="utf-8")

    def test_checkpoint_is_restored_before_published_packages(self) -> None:
        self.assertIn("uses: actions/cache/restore@v6", self.workflow)
        self.assertIn("uses: actions/cache/save@v6", self.workflow)
        self.assertIn("semantic-resume-v2-", self.workflow)
        self.assertLess(
            self.workflow.index("name: Recuperar último avance neuronal"),
            self.workflow.index("name: Restaurar índice anterior"),
        )
        self.assertLess(
            self.workflow.index("name: Restaurar bases de una ejecución incompleta"),
            self.workflow.index("name: Restaurar índice anterior"),
        )
        self.assertIn("hashFiles('data/actas.db.package.json')", self.workflow)
        self.assertIn("github.ref_name", self.workflow)
        self.assertIn(
            "hashFiles('services/database.py', 'services/indexing.py'",
            self.workflow,
        )

    def test_workflow_bounds_each_neural_segment(self) -> None:
        self.assertIn('ACTAS_SEMANTIC_MAX_SECONDS: "7200"', self.workflow)
        self.assertIn('ACTAS_SEMANTIC_MAX_UNIQUE: "50000"', self.workflow)
        self.assertIn('ACTAS_SEMANTIC_NEURAL_BATCH_SIZE: "128"', self.workflow)
        self.assertIn("data/semantic-progress.json", self.workflow)
        self.assertIn("data/semantic.checkpoint.db", self.workflow)

    def test_partial_index_is_not_packaged_or_committed(self) -> None:
        complete_condition = (
            "if: ${{ steps.semantic-state.outputs.complete == 'true' }}"
        )
        for step_name in (
            "Comprimir, verificar y dividir los índices",
            "Restaurar y verificar los paquetes generados",
            "Guardar índice e informe en el repositorio privado",
        ):
            start = self.workflow.index(f"- name: {step_name}")
            block = self.workflow[start : start + 240]
            self.assertIn(complete_condition, block)

    def test_continuation_is_automatic_and_has_a_runaway_guard(self) -> None:
        self.assertIn("actions: write", self.workflow)
        self.assertIn("PROGRESS_MADE", self.workflow)
        self.assertIn("SEGMENTS_COMPLETED", self.workflow)
        self.assertIn('"$SEGMENTS_COMPLETED" -ge 20', self.workflow)
        self.assertIn("gh workflow run build-index.yml", self.workflow)
        self.assertLess(
            self.workflow.index("name: Conservar avance neuronal"),
            self.workflow.index("name: Programar el siguiente segmento"),
        )

    def test_complete_marker_shadows_older_partial_checkpoint(self) -> None:
        self.assertIn("name: Preparar marcador de finalización", self.workflow)
        self.assertIn("name: Cerrar cadena de checkpoints", self.workflow)
        self.assertIn("github.run_attempt", self.workflow)

    def test_confirmed_batches_are_cached_when_the_builder_fails(self) -> None:
        self.assertIn("id: build", self.workflow)
        self.assertIn(
            "failure() && steps.build.outcome == 'failure'",
            self.workflow,
        )
        self.assertIn("name: Preparar recuperación tras un fallo neuronal", self.workflow)
        self.assertIn("name: Conservar recuperación tras un fallo neuronal", self.workflow)
        self.assertIn("steps.failed-checkpoint.outputs.ready == 'true'", self.workflow)


if __name__ == "__main__":
    unittest.main()
